/* =====================================================================
 * js/engine.js — window.Engine
 * Core services: fixed-timestep game loop, keyboard/mouse input state,
 * and the event bus all other modules use to communicate.
 *
 * LOOP   Engine.start([updateFn, renderFn]) / Engine.stop()
 *        'update' (dt) : fixed 60 Hz simulation step (dt === Engine.FIXED_DT)
 *        'render' (dt) : once per animation frame (dt = frame time in s)
 *        Engine.renderAlpha : 0..1 interpolation factor for rendering
 *
 * INPUT  (all on Engine.input)
 *        key(code), keyPressed(code), keyReleased(code)   e.g. 'KeyW', 'Digit3'
 *        mouseButton(b), mousePressed(b), mouseReleased(b)  b: 0=L 1=M 2=R
 *        mouse.dx / mouse.dy : pointer-lock look deltas (reset every update)
 *        axis() : { x, z } movement vector from WASD / arrow keys
 *        clear() : reset all input (also runs automatically on blur)
 *        raw maps: keys, pressed, released, mouse{ x,y,dx,dy,buttons,... }
 *
 * EVENTS 'keydown','keyup','mousedown','mouseup','mousemove'(locked only),
 *        'wheel'(+1/-1),'pointerlock'(bool),'pointerlockerror','blur',
 *        'resize'({width,height}),'fps'(n),'start','stop','pause','resume'
 *
 * FLAGS  autoLock  (true) : clicking the canvas requests pointer lock
 *        autoPause (true) : losing pointer lock (Escape) pauses the
 *        simulation and re-gaining it resumes. A pause set manually
 *        with Engine.setPaused(true) is never auto-resumed.
 * ===================================================================== */
(function () {
    'use strict';

    /* ------------------------------- event bus ------------------------------ */

    const listeners = new Map();

    function on(name, fn) {
        if (typeof fn !== 'function') return function () {};
        if (!listeners.has(name)) listeners.set(name, []);
        listeners.get(name).push(fn);
        return function () { off(name, fn); };
    }

    function once(name, fn) {
        const rm = on(name, function (payload) { rm(); fn(payload); });
        return rm;
    }

    function off(name, fn) {
        const list = listeners.get(name);
        if (!list) return;
        const i = list.indexOf(fn);
        if (i !== -1) list.splice(i, 1);
    }

    function emit(name, payload) {
        const list = listeners.get(name);
        if (!list || list.length === 0) return;
        const snapshot = list.slice();          // safe against off() during emit
        for (let i = 0; i < snapshot.length; i++) {
            try { snapshot[i](payload); }
            catch (err) { console.error('[Engine] "' + name + '" listener threw:', err); }
        }
    }

    /* ------------------------------- input state ----------------------------- */

    const keys         = Object.create(null);   // e.code -> held
    const keysPressed  = Object.create(null);   // e.code -> down since last update
    const keysReleased = Object.create(null);   // e.code -> up since last update

    const mouse = {
        x: 0, y: 0,                             // latest cursor position
        dx: 0, dy: 0,                           // look deltas, cleared each update
        buttons : Object.create(null),          // button -> held
        pressed : Object.create(null),          // button -> down since last update
        released: Object.create(null)           // button -> up since last update
    };

    // Keys whose browser default (scroll / focus move) must never fire.
    const PREVENT = { Space: 1, ArrowUp: 1, ArrowDown: 1, ArrowLeft: 1, ArrowRight: 1, Tab: 1 };

    const isTyping = (e) => {
        const t = e.target;
        return !!t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
    };

    // Edge input (presses / releases / look deltas) lives for exactly one
    // fixed update, so it is never lost between frames nor handled twice.
    function clearEdges() {
        for (const k in keysPressed)  keysPressed[k] = false;
        for (const k in keysReleased) keysReleased[k] = false;
        for (const b in mouse.pressed)  mouse.pressed[b] = false;
        for (const b in mouse.released) mouse.released[b] = false;
        mouse.dx = 0;
        mouse.dy = 0;
    }

    function clearInput() {
        for (const k in keys) keys[k] = false;
        for (const b in mouse.buttons) mouse.buttons[b] = false;
        clearEdges();
    }

    /* ------------------------------ DOM handlers ----------------------------- */

    document.addEventListener('keydown', (e) => {
        if (isTyping(e)) return;
        if (!e.repeat) {
            keysPressed[e.code] = true;
            emit('keydown', e);
        }
        keys[e.code] = true;
        if (PREVENT[e.code] ||
            (Engine.pointerLocked && !e.metaKey && !e.ctrlKey && !e.altKey &&
             e.key !== 'Escape' && !/^F\d/.test(e.key))) {
            e.preventDefault();
        }
    });

    document.addEventListener('keyup', (e) => {
        if (isTyping(e)) return;
        keys[e.code] = false;
        keysReleased[e.code] = true;
        emit('keyup', e);
    });

    document.addEventListener('mousemove', (e) => {
        mouse.x = e.clientX;
        mouse.y = e.clientY;
        if (Engine.pointerLocked) {             // only look around while locked
            mouse.dx += e.movementX || 0;
            mouse.dy += e.movementY || 0;
            emit('mousemove', e);
        }
    });

    // While unlocked, only canvas / page-background clicks count as game
    // input; clicks on HUD or menu elements are left for the UI module.
    const targetsGame = (e) =>
        Engine.pointerLocked || e.target === getCanvas() ||
        e.target === document.body || e.target === document.documentElement;

    document.addEventListener('mousedown', (e) => {
        if (isTyping(e) || !targetsGame(e)) return;
        e.preventDefault();
        mouse.buttons[e.button] = true;
        mouse.pressed[e.button] = true;
        if (Engine.autoLock && Engine.running && !Engine.pointerLocked) lockPointer();
        emit('mousedown', e);
    });

    document.addEventListener('mouseup', (e) => {
        if (!mouse.buttons[e.button]) return;
        mouse.buttons[e.button] = false;
        mouse.released[e.button] = true;
        emit('mouseup', e);
    });

    document.addEventListener('contextmenu', (e) => e.preventDefault()); // right-click places

    document.addEventListener('wheel', (e) => {
        if (!Engine.pointerLocked) return;
        e.preventDefault();
        emit('wheel', e.deltaY > 0 ? 1 : e.deltaY < 0 ? -1 : 0);
    }, { passive: false });

    window.addEventListener('blur', () => { clearInput(); emit('blur'); });

    window.addEventListener('resize', () =>
        emit('resize', { width: window.innerWidth, height: window.innerHeight }));

    document.addEventListener('visibilitychange', () => {
        if (document.hidden) clearInput();      // no stuck keys after tab-out
    });

    /* ------------------------------ pointer lock ----------------------------- */

    function getCanvas() { return document.getElementById('game-canvas'); }

    function lockPointer() {
        const c = getCanvas();
        if (!c || !c.requestPointerLock) return;
        try {
            const p = c.requestPointerLock();   // may reject during re-lock cooldown
            if (p && typeof p.catch === 'function') p.catch(function () {});
        } catch (err) { /* ignored */ }
    }

    function unlockPointer() { if (document.exitPointerLock) document.exitPointerLock(); }

    let everLocked = false;   // has a pointer lock ever been acquired?
    let autoPaused = false;   // was the current pause caused by autoPause?

    document.addEventListener('pointerlockchange', () => {
        const locked = !!document.pointerLockElement;
        Engine.pointerLocked = locked;
        clearEdges();                          // never carry clicks across lock changes
        if (locked) {
            everLocked = true;
            if (autoPaused) { autoPaused = false; setPaused(false); }
        } else if (Engine.autoPause && everLocked && !Engine.paused) {
            autoPaused = true;                 // Escape drops the lock -> pause
            setPaused(true);
        }
        emit('pointerlock', locked);
    });

    document.addEventListener('pointerlockerror', () => emit('pointerlockerror'));

    /* --------------------------------- pause --------------------------------- */

    function setPaused(v) {
        v = !!v;
        if (Engine.paused === v) return;
        Engine.paused = v;
        clearEdges();
        emit(v ? 'pause' : 'resume');
    }

    /* --------------------------- fixed-timestep loop ------------------------- */

    const FIXED_DT     = 1 / 60;   // seconds per simulation step
    const MAX_FRAME_DT = 0.1;      // clamp long frame gaps (hitches, tab switch)
    const MAX_STEPS    = 6;        // updates per frame cap (spiral-of-death guard)

    let running = false;
    let rafId = 0;
    let last = 0;
    let accumulator = 0;
    let frames = 0;
    let fpsWindow = 0;

    const stats = { fps: 0, frameMs: 0, updates: 0, time: 0 };

    function tick(now) {
        if (!running) return;
        rafId = requestAnimationFrame(tick);

        let dt = (now - last) / 1000;
        last = now;
        if (!(dt > 0)) dt = 0;
        if (dt > MAX_FRAME_DT) dt = MAX_FRAME_DT;
        stats.frameMs = dt * 1000;

        if (!Engine.paused) {
            accumulator += dt;
            let steps = 0;
            while (accumulator >= FIXED_DT && steps < MAX_STEPS) {
                emit('update', FIXED_DT);
                stats.updates++;
                stats.time += FIXED_DT;
                accumulator -= FIXED_DT;
                steps++;
                clearEdges();                 // edges last exactly one update
            }
            if (steps === MAX_STEPS) accumulator = 0;  // drop unpayable backlog
            Engine.renderAlpha = accumulator / FIXED_DT;
        } else {
            clearEdges();                     // discard pending input while paused
        }

        emit('render', dt);                   // keep rendering while paused (menus)

        frames++;
        fpsWindow += dt;
        if (fpsWindow >= 0.5) {
            stats.fps = Math.round(frames / fpsWindow);
            frames = 0;
            fpsWindow = 0;
            emit('fps', stats.fps);
        }
    }

    function start(updateFn, renderFn) {
        if (running) return Engine;
        if (typeof updateFn === 'function') on('update', updateFn);
        if (typeof renderFn === 'function') on('render', renderFn);
        running = true;
        last = performance.now();
        accumulator = 0;
        rafId = requestAnimationFrame(tick);
        emit('start');
        emit('resize', { width: window.innerWidth, height: window.innerHeight });
        return Engine;
    }

    function stop() {
        if (!running) return Engine;
        running = false;
        cancelAnimationFrame(rafId);
        clearInput();
        emit('stop');
        return Engine;
    }

    /* ------------------------------- public API ------------------------------ */

    const input = {
        keys: keys, pressed: keysPressed, released: keysReleased, mouse: mouse,
        key: (code) => !!keys[code],
        keyPressed: (code) => !!keysPressed[code],
        keyReleased: (code) => !!keysReleased[code],
        mouseButton: (b) => !!mouse.buttons[b],
        mousePressed: (b) => !!mouse.pressed[b],
        mouseReleased: (b) => !!mouse.released[b],
        axis: () => {                          // z: -1 forward, +1 back
            let x = 0, z = 0;
            if (keys.KeyW || keys.ArrowUp) z -= 1;
            if (keys.KeyS || keys.ArrowDown) z += 1;
            if (keys.KeyA || keys.ArrowLeft) x -= 1;
            if (keys.KeyD || keys.ArrowRight) x += 1;
            return { x: x, z: z };
        },
        clear: clearInput
    };

    window.Engine = {
        on: on, once: once, off: off, emit: emit,          // event bus
        start: start, stop: stop,                          // loop control
        FIXED_DT: FIXED_DT, DT: FIXED_DT, renderAlpha: 0,
        get running() { return running; },
        get time() { return stats.time; },
        setPaused: setPaused, togglePause: () => setPaused(!Engine.paused),
        paused: false,
        lockPointer: lockPointer, unlockPointer: unlockPointer, getCanvas: getCanvas,
        pointerLocked: false,
        autoLock: true,     // clicking the canvas acquires pointer lock
        autoPause: true,    // losing pointer lock after play began pauses the game
        input: input, keys: keys, mouse: mouse,
        key: input.key, keyPressed: input.keyPressed,
        stats: stats,       // { fps, frameMs, updates, time } — for the FPS counter
        version: '1.0.0'
    };
})();
