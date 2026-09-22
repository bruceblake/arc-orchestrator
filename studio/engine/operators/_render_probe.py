"""Runs INSIDE Blender: render the scene to a PNG so the model can SEE it.

    blender -b <file.blend> -P _render_probe.py -- <out.png> [camera] [WxH] [engine]

This is the step that makes script-driven 3D good rather than merely correct.
Published accounts of GPT-6 Astra's Blender workflow (2026-09) describe it as
writing bpy, then rendering frames "to inspect the outcome", then revising its
own script "wherever the render diverges from the brief". A modelling agent
that never looks at what it built is writing geometry blind; this probe is the
eyes.

With no camera in the scene, one is created that frames every mesh from a
three-quarter view — the angle that reveals silhouette, depth and proportion
at once, where a front view hides depth and a top view hides height.
"""
import math
import sys

MARK_OPEN = "<<<STUDIO_RENDER_JSON"
MARK_CLOSE = "STUDIO_RENDER_JSON>>>"


def _args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    out = argv[0] if argv else "/tmp/studio-render.png"
    camera = argv[1] if len(argv) > 1 and argv[1] != "-" else ""
    res = argv[2] if len(argv) > 2 else "1280x720"
    engine = argv[3] if len(argv) > 3 else "BLENDER_EEVEE_NEXT"
    w, _, h = res.partition("x")
    return out, camera, int(w or 1280), int(h or 720), engine


def _bounds(meshes):
    from mathutils import Vector
    lo = Vector((math.inf, math.inf, math.inf))
    hi = Vector((-math.inf, -math.inf, -math.inf))
    for obj in meshes:
        for corner in obj.bound_box:
            p = obj.matrix_world @ Vector(corner)
            lo = Vector((min(lo.x, p.x), min(lo.y, p.y), min(lo.z, p.z)))
            hi = Vector((max(hi.x, p.x), max(hi.y, p.y), max(hi.z, p.z)))
    return lo, hi


def _frame_camera(scene, meshes):
    """A three-quarter camera that contains every mesh in frame."""
    import bpy
    from mathutils import Vector
    lo, hi = _bounds(meshes)
    centre = (lo + hi) / 2
    radius = max((hi - lo).length / 2, 0.5)
    cam_data = bpy.data.cameras.new("StudioPreviewCam")
    cam_data.lens = 50
    cam = bpy.data.objects.new("StudioPreviewCam", cam_data)
    scene.collection.objects.link(cam)
    # Distance that fits the bounding sphere inside a 50mm lens's field of view.
    fov = 2 * math.atan(cam_data.sensor_width / (2 * cam_data.lens))
    dist = radius / math.sin(fov / 2) * 1.15
    direction = Vector((1.0, -1.0, 0.75)).normalized()
    cam.location = centre + direction * dist
    look = centre - cam.location
    cam.rotation_euler = look.to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam
    return cam


def _ensure_light(scene):
    """A graybox needs SOME light or every render is black."""
    import bpy
    if any(o.type == "LIGHT" for o in scene.objects):
        return
    sun = bpy.data.lights.new("StudioPreviewSun", "SUN")
    sun.energy = 3.0
    obj = bpy.data.objects.new("StudioPreviewSun", sun)
    obj.rotation_euler = (math.radians(50), 0, math.radians(35))
    scene.collection.objects.link(obj)


def main():
    import bpy
    out, camera, w, h, engine = _args()
    scene = bpy.context.scene
    meshes = [o for o in scene.objects if o.type == "MESH"]
    report = {"out": out, "meshes": len(meshes)}
    if not meshes:
        report["error"] = "the scene contains no meshes to render"
        return report
    if camera and camera in bpy.data.objects:
        scene.camera = bpy.data.objects[camera]
        report["camera"] = camera
    elif scene.camera is None:
        _frame_camera(scene, meshes)
        report["camera"] = "StudioPreviewCam (auto-framed three-quarter)"
    else:
        report["camera"] = scene.camera.name
    _ensure_light(scene)
    # EEVEE when this Blender has it (materials and light read true); the
    # Workbench engine as a fallback, which needs no GPU and still shows
    # geometry, silhouette and proportion — the things a graybox is judged on.
    engines = [e.identifier for e in
               bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items]
    scene.render.engine = engine if engine in engines else (
        "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in engines else "BLENDER_WORKBENCH")
    report["engine"] = scene.render.engine
    scene.render.resolution_x, scene.render.resolution_y = w, h
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = out
    bpy.ops.render.render(write_still=True)
    report["resolution"] = f"{w}x{h}"
    return report


if __name__ == "__main__":
    import json
    try:
        result = main()
    except Exception as exc:                                  # noqa: BLE001
        result = {"error": f"{type(exc).__name__}: {exc}"}
    print(MARK_OPEN)
    print(json.dumps(result, default=str))
    print(MARK_CLOSE)
