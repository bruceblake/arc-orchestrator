extends CanvasLayer
## ARC human-playtest overlay (studio/playtest.py injects it as an autoload).
##
## Lives ONLY in a playtest snapshot, never in the game repo. Inert unless the
## game was launched with `-- --arc-playtest-dir=<dir>`: then F8 captures the
## frame, pauses the game and asks for a note; the finding is appended as one
## JSON line to <dir>/findings.jsonl, and a telemetry line goes to
## <dir>/telemetry.jsonl every 5 s. The orchestrator ingests both files; this
## script never talks to anything else.

const CATEGORIES := ["bug", "feel", "balance", "ux", "visual", "perf", "other"]
const SEVERITIES := ["1 blocker", "2 major", "3 minor", "4 polish"]
const TELEMETRY_SECONDS := 5.0

var _dir := ""
var _build := ""
var _shot_n := 0
var _shot := ""
var _paused_before := false
var _mouse_before := Input.MOUSE_MODE_VISIBLE
var _panel: PanelContainer
var _note: LineEdit
var _category: OptionButton
var _severity: OptionButton


func _ready() -> void:
	for arg in OS.get_cmdline_user_args():
		if arg.begins_with("--arc-playtest-dir="):
			_dir = arg.substr("--arc-playtest-dir=".length())
		elif arg.begins_with("--arc-build="):
			_build = arg.substr("--arc-build=".length())
	if _dir == "":
		queue_free()
		return
	process_mode = Node.PROCESS_MODE_ALWAYS
	layer = 128
	DirAccess.make_dir_recursive_absolute(_dir)
	while FileAccess.file_exists(_dir.path_join("shot-%d.png" % _shot_n)):
		_shot_n += 1
	_build_ui()
	# The graybox ships with no lights on purpose. The render harness adds a
	# sun for screenshots; without the same light a human playtest is a black
	# window with only this overlay's label on it.
	call_deferred("_ensure_inspection_light")
	var timer := Timer.new()
	timer.wait_time = TELEMETRY_SECONDS
	timer.autostart = true
	timer.timeout.connect(_telemetry)
	add_child(timer)
	_telemetry()


func _build_ui() -> void:
	var tag := Label.new()
	tag.text = "PLAYTEST %s · F8 = report" % _build.substr(0, 7)
	tag.position = Vector2(8, 6)
	tag.modulate = Color(1, 1, 1, 0.6)
	add_child(tag)

	_panel = PanelContainer.new()
	_panel.set_anchors_preset(Control.PRESET_CENTER)
	_panel.custom_minimum_size = Vector2(480, 0)
	_panel.visible = false
	add_child(_panel)
	var box := VBoxContainer.new()
	_panel.add_child(box)
	var title := Label.new()
	title.text = "Playtest finding (Enter = save, Esc = cancel)"
	box.add_child(title)
	_note = LineEdit.new()
	_note.placeholder_text = "What happened? What did you expect?"
	_note.max_length = 2000
	_note.text_submitted.connect(func(_t: String) -> void: _save())
	box.add_child(_note)
	var row := HBoxContainer.new()
	box.add_child(row)
	_category = OptionButton.new()
	for c in CATEGORIES:
		_category.add_item(c)
	row.add_child(_category)
	_severity = OptionButton.new()
	for s in SEVERITIES:
		_severity.add_item(s)
	_severity.select(2)
	row.add_child(_severity)
	var buttons := HBoxContainer.new()
	box.add_child(buttons)
	var save := Button.new()
	save.text = "Save"
	save.pressed.connect(_save)
	buttons.add_child(save)
	var cancel := Button.new()
	cancel.text = "Cancel"
	cancel.pressed.connect(_close)
	buttons.add_child(cancel)


func _ensure_inspection_light() -> void:
	var root := get_tree().current_scene
	if root == null:
		return
	if root.find_children("*", "Light3D", true, false).is_empty():
		var sun := DirectionalLight3D.new()
		sun.name = "PlaytestInspectionSun"
		sun.rotation_degrees = Vector3(-50.0, 35.0, 0.0)
		sun.light_energy = 1.2
		sun.shadow_enabled = true
		root.add_child(sun)
	if root.find_children("*", "WorldEnvironment", true, false).is_empty():
		var env := Environment.new()
		var sky := Sky.new()
		sky.sky_material = ProceduralSkyMaterial.new()
		env.background_mode = Environment.BG_SKY
		env.sky = sky
		env.ambient_light_source = Environment.AMBIENT_SOURCE_SKY
		env.ambient_light_energy = 0.6
		var we := WorldEnvironment.new()
		we.name = "PlaytestInspectionEnvironment"
		we.environment = env
		root.add_child(we)


func _input(event: InputEvent) -> void:
	if _panel != null and not _panel.visible and event is InputEventMouseButton \
			and event.pressed and event.button_index == MOUSE_BUTTON_LEFT \
			and Input.mouse_mode != Input.MOUSE_MODE_CAPTURED:
		# The player never captures the mouse itself, so look never starts.
		Input.mouse_mode = Input.MOUSE_MODE_CAPTURED
		get_viewport().set_input_as_handled()
		return
	if not (event is InputEventKey) or not event.pressed or event.echo:
		return
	if _panel.visible:
		if event.keycode == KEY_ESCAPE:
			_close()
			get_viewport().set_input_as_handled()
		return
	if event.keycode == KEY_F8:
		_open()
		get_viewport().set_input_as_handled()


func _open() -> void:
	# The frame FIRST, before the panel covers it.
	_shot = ""
	var tex := get_viewport().get_texture()
	var img: Image = tex.get_image() if tex != null else null
	if img != null and not img.is_empty():
		var shot_name := "shot-%d.png" % _shot_n
		if img.save_png(_dir.path_join(shot_name)) == OK:
			_shot = shot_name
			_shot_n += 1
	_paused_before = get_tree().paused
	_mouse_before = Input.mouse_mode
	get_tree().paused = true
	Input.mouse_mode = Input.MOUSE_MODE_VISIBLE
	_note.text = ""
	_panel.visible = true
	_note.grab_focus()


func _close() -> void:
	_panel.visible = false
	get_tree().paused = _paused_before
	Input.mouse_mode = _mouse_before


func _save() -> void:
	_append("findings.jsonl", {
		"ts": Time.get_unix_time_from_system(),
		"note": _note.text.strip_edges(),
		"category": CATEGORIES[_category.selected],
		"severity": _severity.selected + 1,
		"scene": _scene(),
		"screenshot": _shot if _shot != "" else null,
		"game_time": Time.get_ticks_msec() / 1000.0,
		"fps": Engine.get_frames_per_second(),
	})
	_close()


func _telemetry() -> void:
	_append("telemetry.jsonl", {
		"ts": Time.get_unix_time_from_system(),
		"scene": _scene(),
		"fps": Engine.get_frames_per_second(),
		"game_time": Time.get_ticks_msec() / 1000.0,
	})


func _scene() -> String:
	var cur := get_tree().current_scene
	return cur.scene_file_path if cur != null else ""


func _append(file: String, data: Dictionary) -> void:
	var path := _dir.path_join(file)
	var f := FileAccess.open(path, FileAccess.READ_WRITE if FileAccess.file_exists(path) else FileAccess.WRITE)
	if f == null:
		push_warning("arc playtest: cannot write %s" % path)
		return
	f.seek_end()
	f.store_line(JSON.stringify(data))
	f.close()
