import asyncio
import json
import logging
import random

import config
import events
from config import FAMILY_ORDER
from graph import Graph

log = logging.getLogger("work")


class Roles:
    def __init__(self):
        self.i = 0

    def next(self):
        i = self.i
        self.i += 1
        fams = FAMILY_ORDER
        return {
            "questions": fams[i % 4],
            "synthesize": fams[(i + 1) % 4],
            "verify": fams[(i + 2) % 4],
            "seeds": fams[(i + 3) % 4],
        }


def parse_json_loose(text):
    t = text.strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = t.find(opener), t.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except Exception:
                pass
    raise ValueError(f"no parseable JSON in: {text[:200]!r}")


def build_round_graph(pool, store, roles, meta, *, questions=None, seeds=None):
    qn = questions or config.QUESTIONS_PER_ROUND
    seed_n = seeds or config.SEEDS_PER_ROUND

    async def pick_topic(ctx):
        topic = store.take_seed()
        if topic is None:
            topic = random.choice(config.DEFAULT_SEEDS)
        r = roles.next()
        rid = store.start_round(topic)
        meta["round_id"] = rid
        meta["topic"] = topic
        log.info(
            "round %s — topic: %s (questioner=%s synthesizer=%s verifier=%s)",
            meta.get("round_index"), topic, r["questions"], r["synthesize"], r["verify"],
        )
        events.emit("round_start", round_id=rid, topic=topic, roles=r,
                    round_index=meta.get("round_index"))
        return {"topic": topic, "roles": r, "round_id": rid}

    async def gen_questions(ctx):
        head = ctx["results"]["pick_topic"]
        topic, r, rid = head["topic"], head["roles"], head["round_id"]
        prompt = (
            f"Generate {qn} diverse, specific, open research questions about: {topic}. "
            "Prefer questions where current, verifiable information matters. "
            'Return ONLY JSON: {"questions": ["...", "..."]}'
        )
        text = await pool.chat(r["questions"], [{"role": "user", "content": prompt}], purpose="questions")
        data = parse_json_loose(text)
        qs = [str(x).strip() for x in data.get("questions", []) if str(x).strip()][:qn]
        if not qs:
            raise RuntimeError(f"no questions parsed for topic: {topic}")
        item_ids = store.add_items(rid, qs)
        log.info("generated %d questions for %r", len(qs), topic)
        return {"topic": topic, "questions": qs, "item_ids": item_ids, "roles": r, "round_id": rid}

    def make_answer(fam):
        async def answer(ctx):
            gq = ctx["results"]["gen_questions"]

            async def one(i, q):
                prompt = (
                    "Answer the question below thoroughly but concisely (under 400 words). "
                    "State key claims explicitly so they can be fact-checked.\n\n"
                    f"Question: {q}"
                )
                text = await pool.chat(fam, [{"role": "user", "content": prompt}], purpose="answer")
                return i, text

            pairs = await asyncio.gather(*(one(i, q) for i, q in enumerate(gq["questions"])))
            return {fam: dict(pairs)}

        return answer

    async def research_web(ctx):
        gq = ctx["results"]["gen_questions"]
        base = meta.get("round_index", 0)

        async def one(i, q):
            fam = FAMILY_ORDER[(base + i) % len(FAMILY_ORDER)]
            prompt = (
                "Research the question below. Search on the web for current, authoritative information. "
                "Summarize the key findings and cite sources (name and date). "
                "If nothing authoritative exists online, say so.\n\n"
                f"Question: {q}"
            )
            text = await pool.chat(fam, [{"role": "user", "content": prompt}], websearch=True, purpose="research")
            return i, {"family": fam, "text": text}

        pairs = await asyncio.gather(*(one(i, q) for i, q in enumerate(gq["questions"])))
        log.info("web research done for %d questions", len(pairs))
        return {"research": dict(pairs)}

    async def critique(ctx):
        gq = ctx["results"]["gen_questions"]
        qs, item_ids = gq["questions"], gq["item_ids"]
        research = ctx["results"]["research_web"]["research"]
        answers = {fam: ctx["results"][f"answer_{fam}"][fam] for fam in FAMILY_ORDER}
        base = meta.get("round_index", 0)

        async def one(qi, fam):
            q = qs[qi]
            ev = research.get(qi, {}).get("text", "(no web research)")
            critics = [f for f in FAMILY_ORDER if f != fam]
            cf = critics[(base + qi) % len(critics)]
            prompt = (
                "You are a skeptical fact-checker. Score the answer below against the web evidence.\n"
                'Return ONLY JSON: {"score": <0-10>, "verdict": "solid"|"flawed", "issues": "<one sentence>"}\n\n'
                f"Question: {q}\n\nAnswer (written by {fam}):\n{answers[fam][qi]}\n\nWeb evidence:\n{ev}"
            )
            text = await pool.chat(cf, [{"role": "user", "content": prompt}], purpose="critique")
            try:
                d = parse_json_loose(text)
                score = float(d.get("score", 5))
                verdict = str(d.get("verdict", "flawed"))
                issues = str(d.get("issues", ""))[:500]
            except Exception:
                score, verdict, issues = 5.0, "flawed", "critique unparseable"
            store.save_answer(item_ids[qi], fam, answers[fam][qi], cf, score, verdict, issues)
            return (qi, fam), {"critic": cf, "score": score, "verdict": verdict, "issues": issues}

        jobs = [one(qi, fam) for qi in range(len(qs)) for fam in FAMILY_ORDER]
        pairs = await asyncio.gather(*jobs)
        log.info("cross-critique done: %d answer reviews", len(pairs))
        return {"critiques": dict(pairs)}

    async def synthesize(ctx):
        gq = ctx["results"]["gen_questions"]
        head = ctx["results"]["pick_topic"]
        qs = gq["questions"]
        research = ctx["results"]["research_web"]["research"]
        answers = {fam: ctx["results"][f"answer_{fam}"][fam] for fam in FAMILY_ORDER}
        critiques = ctx["results"]["critique"]["critiques"]
        fam = head["roles"]["synthesize"]
        attempt = ctx.get("verify_round", 0) + 1
        feedback = ctx.get("feedback", {})

        async def one(qi):
            q = qs[qi]
            ans_block = "\n\n".join(f"{f} wrote:\n{answers[f][qi]}" for f in FAMILY_ORDER)
            crit_block = "\n".join(
                f"- {f}: {critiques[(qi, f)]['score']}/10 ({critiques[(qi, f)]['verdict']}): {critiques[(qi, f)]['issues']}"
                for f in FAMILY_ORDER
            )
            fb = feedback.get(qi)
            fb_block = (
                f"\n\nA previous synthesis attempt was rejected by verification with this feedback: {fb}\n"
                "Produce a corrected, stronger version."
            ) if fb else ""
            prompt = (
                "You are the synthesizer. Merge the four model answers and the web evidence into one final "
                "answer (under 500 words). Prefer claims supported by the web evidence and flag disagreements "
                f"between models explicitly.{fb_block}\n\n"
                f"Question: {q}\n\n{ans_block}\n\nWeb evidence:\n{research.get(qi, {}).get('text', '')}\n\n"
                f"Critiques:\n{crit_block}"
            )
            text = await pool.chat(fam, [{"role": "user", "content": prompt}], purpose="synthesis")
            return qi, text

        pairs = await asyncio.gather(*(one(qi) for qi in range(len(qs))))
        ctx["verify_round"] = attempt
        log.info("synthesis attempt %d complete (synthesizer=%s)", attempt, fam)
        return {"syntheses": dict(pairs), "attempt": attempt}

    async def verify(ctx):
        head = ctx["results"]["pick_topic"]
        qs = ctx["results"]["gen_questions"]["questions"]
        syntheses = ctx["results"]["synthesize"]["syntheses"]
        research = ctx["results"]["research_web"]["research"]
        critiques = ctx["results"]["critique"]["critiques"]
        fam = head["roles"]["verify"]
        round_n = ctx.get("verify_round", 1)

        async def one(qi):
            q = qs[qi]
            prompt = (
                "You are an independent verifier. Judge whether the final answer below is accurate and "
                "well-grounded in the web evidence, given the earlier critiques. Be strict.\n"
                'Return ONLY JSON: {"passed": true|false, "score": <0-10>, "feedback": "<what to fix if failed>"}\n\n'
                f"Question: {q}\n\nFinal answer:\n{syntheses[qi]}\n\n"
                f"Web evidence:\n{research.get(qi, {}).get('text', '')}\n\n"
                f"Prior critiques: {[critiques[(qi, f)]['issues'] for f in FAMILY_ORDER]}"
            )
            text = await pool.chat(fam, [{"role": "user", "content": prompt}], purpose="verify")
            try:
                d = parse_json_loose(text)
                score = float(d.get("score", 0))
                passed = score >= config.VERIFY_PASS_SCORE
                fb = str(d.get("feedback", ""))[:500]
            except Exception:
                score, passed, fb = 0.0, False, "verifier output unparseable"
            return qi, {"passed": passed, "score": score, "feedback": fb}

        pairs = await asyncio.gather(*(one(qi) for qi in range(len(qs))))
        verdicts = dict(pairs)
        failed = {qi: v["feedback"] for qi, v in verdicts.items() if not v["passed"]}
        ctx["feedback"] = failed
        for qi, v in verdicts.items():
            events.emit("verify", question_index=qi, passed=v["passed"], score=v["score"],
                        round_n=round_n)
        if failed:
            log.info("verify round %d: %d/%d failed (verifier=%s)", round_n, len(failed), len(qs), fam)
        return {"passed": not failed, "round": round_n, "verdicts": verdicts}

    async def store_results(ctx):
        gq = ctx["results"]["gen_questions"]
        head = ctx["results"]["pick_topic"]
        qs, item_ids = gq["questions"], gq["item_ids"]
        syntheses = ctx["results"]["synthesize"]["syntheses"]
        verdicts = ctx["results"]["verify"]["verdicts"]
        rounds_used = ctx.get("verify_round", 1)
        for qi in range(len(qs)):
            store.save_final(item_ids[qi], syntheses[qi], verdicts[qi]["score"], verdicts[qi]["passed"], rounds_used)
        fam = head["roles"]["seeds"]
        findings = "\n".join(f"- {q}" for q in qs)
        prompt = (
            f"The topic was '{head['topic']}'. Given the questions already investigated below, propose "
            f"{seed_n} NEW specific topics or questions worth investigating next (no duplicates).\n"
            'Return ONLY JSON: {"topics": ["...", "..."]}\n\nAlready investigated:\n' + findings
        )
        text = await pool.chat(fam, [{"role": "user", "content": prompt}], purpose="seeds")
        try:
            topics = [str(t).strip() for t in parse_json_loose(text).get("topics", []) if str(t).strip()]
        except Exception:
            topics = []
        topics = topics[:seed_n]
        if topics:
            store.add_seeds(head["round_id"], topics)
        log.info("stored %d items, %d new seeds", len(qs), len(topics))
        return {
            "stored": len(qs),
            "seeds": len(topics),
            "verify_rounds": rounds_used,
            "passed_all": ctx["results"]["verify"]["passed"],
        }

    g = Graph("round", max_steps=config.MAX_GRAPH_STEPS)
    g.node("pick_topic", pick_topic)
    g.node("gen_questions", gen_questions)
    for fam in FAMILY_ORDER:
        g.node(f"answer_{fam}", make_answer(fam))
    g.node("research_web", research_web)
    g.node("critique", critique, gather=True)
    g.node("synthesize", synthesize)
    g.node("verify", verify)
    g.node("store_results", store_results)

    g.start("pick_topic")
    g.edge("pick_topic", "gen_questions")
    for fam in FAMILY_ORDER:
        g.edge("gen_questions", f"answer_{fam}")
    g.edge("gen_questions", "research_web")
    for src in [f"answer_{fam}" for fam in FAMILY_ORDER] + ["research_web"]:
        g.edge(src, "critique")
    g.edge("critique", "synthesize")
    g.edge("synthesize", "verify")
    g.edge("verify", "store_results", when=lambda r, ctx: r["passed"] or r["round"] >= config.MAX_VERIFY_ROUNDS)
    g.edge("verify", "synthesize", when=lambda r, ctx: (not r["passed"]) and r["round"] < config.MAX_VERIFY_ROUNDS)
    return g