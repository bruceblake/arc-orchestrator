"""Runs INSIDE Blender (`blender -b -P _mesh_probe.py -- <path> [budget]`).

Imports one asset and prints a JSON report between markers. It is a separate
file rather than a string in the operator so it can be linted, diffed and
fixed like code — a 200-line Python program embedded in another module's
string literal is a program nobody maintains.

Everything reported here is MEASURED. Where a metric is an approximation it
says so in the report itself (`uv_overlap_method`), because a mesh report that
overstates its own certainty is worse than no report: it ends an argument that
should have continued.
"""
import json
import math
import os
import sys

MARK_OPEN = "<<<STUDIO_MESH_JSON"
MARK_CLOSE = "STUDIO_MESH_JSON>>>"


def _args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    path = argv[0] if argv else ""
    budget = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else 0
    return path, budget


def _import(path):
    import bpy
    bpy.ops.wm.read_factory_settings(use_empty=True)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".gltf", ".glb"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        # Blender 4+/5 renamed the operator; support both.
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=path)
        else:
            bpy.ops.import_scene.obj(filepath=path)
    elif ext == ".dae":
        bpy.ops.wm.collada_import(filepath=path)
    elif ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=path)
    else:
        raise ValueError("unsupported asset type: %r" % ext)


def _uv_overlap(mesh, grid=64, cap=200000):
    """Approximate UV overlap: triangles whose UV bounding boxes collide.

    A true overlap test is a polygon intersection over every pair, which is
    O(n^2) and far too slow for an asset budget measured in tens of
    thousands of triangles. This buckets each UV triangle's bounding box into
    a grid and counts colliding pairs inside a cell. It OVER-reports (two
    boxes can overlap while the triangles do not) and the report says so, so
    a nonzero number here is a prompt to look, not a verdict.
    """
    uv = mesh.uv_layers.active
    if uv is None:
        return {"uv_overlap_pairs": None, "uv_missing": True}
    mesh.calc_loop_triangles()
    cells = {}
    pairs = 0
    checked = 0
    out_of_range = 0
    for tri in mesh.loop_triangles:
        us, vs = [], []
        for li in tri.loops:
            u, v = uv.data[li].uv
            us.append(u)
            vs.append(v)
        lo = (min(us), min(vs))
        hi = (max(us), max(vs))
        if lo[0] < -0.001 or lo[1] < -0.001 or hi[0] > 1.001 or hi[1] > 1.001:
            out_of_range += 1
        cx0 = max(0, min(grid - 1, int(lo[0] * grid)))
        cy0 = max(0, min(grid - 1, int(lo[1] * grid)))
        cx1 = max(0, min(grid - 1, int(hi[0] * grid)))
        cy1 = max(0, min(grid - 1, int(hi[1] * grid)))
        box = (lo[0], lo[1], hi[0], hi[1])
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                bucket = cells.setdefault((cx, cy), [])
                for other in bucket:
                    checked += 1
                    if checked > cap:
                        return {"uv_overlap_pairs": pairs,
                                "uv_overlap_truncated": True,
                                "uv_out_of_range_tris": out_of_range,
                                "uv_missing": False}
                    if not (box[2] < other[0] or other[2] < box[0]
                            or box[3] < other[1] or other[3] < box[1]):
                        pairs += 1
                bucket.append(box)
    return {"uv_overlap_pairs": pairs, "uv_overlap_truncated": False,
            "uv_out_of_range_tris": out_of_range, "uv_missing": False}


def _bones(armature_obj):
    bones = []
    for b in armature_obj.data.bones:
        depth, parent = 0, b.parent
        while parent is not None:
            depth += 1
            parent = parent.parent
        bones.append({"name": b.name,
                      "parent": b.parent.name if b.parent else None,
                      "depth": depth})
    roots = [b["name"] for b in bones if b["parent"] is None]
    return {"bone_count": len(bones), "roots": roots,
            "max_depth": max([b["depth"] for b in bones] or [0]),
            "bones": bones[:400]}


def main():
    import bpy
    import bmesh
    path, budget = _args()
    report = {"model_path": path, "max_triangle_count": budget}
    if not path or not os.path.exists(path):
        report["error"] = "asset not found: %r" % path
        return report
    _import(path)
    tris = 0
    verts = 0
    non_manifold = 0
    loose = 0
    ngons = 0
    meshes = []
    uv_info = {}
    materials = set()
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        mesh = obj.data
        mesh.calc_loop_triangles()
        t = len(mesh.loop_triangles)
        tris += t
        verts += len(mesh.vertices)
        ngons += sum(1 for p in mesh.polygons if len(p.vertices) > 4)
        meshes.append({"name": obj.name, "triangles": t,
                       "vertices": len(mesh.vertices)})
        for slot in obj.material_slots:
            if slot.material:
                materials.add(slot.material.name)
        bm = bmesh.new()
        bm.from_mesh(mesh)
        non_manifold += sum(1 for e in bm.edges if not e.is_manifold)
        loose += sum(1 for v in bm.verts if not v.link_edges)
        bm.free()
        if not uv_info:
            uv_info = _uv_overlap(mesh)
    report.update({
        "triangles": tris,
        "vertices": verts,
        "mesh_count": len(meshes),
        "meshes": meshes[:200],
        "non_manifold_edges": non_manifold,
        "loose_vertices": loose,
        "ngons": ngons,
        "materials": sorted(materials),
        "material_count": len(materials),
        "uv_overlap_method": "bounding-box grid (approximate, over-reports)",
    })
    report.update(uv_info)
    armatures = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    report["armature_count"] = len(armatures)
    report["skeleton"] = _bones(armatures[0]) if armatures else None
    actions = [a.name for a in bpy.data.actions]
    report["actions"] = actions[:200]
    report["action_count"] = len(actions)
    if budget and tris > budget:
        report["over_budget_by"] = tris - budget
    return report


if __name__ == "__main__":
    try:
        out = main()
    except Exception as exc:                                 # noqa: BLE001
        out = {"error": "%s: %s" % (type(exc).__name__, exc)}
    print(MARK_OPEN)
    print(json.dumps(out, indent=2, default=str))
    print(MARK_CLOSE)
