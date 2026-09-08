/* ==========================================================================
 * main.js — bootstrap module for the voxel sandbox.
 *
 * Owns: THREE renderer / scene / camera, sky + fog, simple directional
 * lighting, the fixed-timestep update loop, resize handling, and the pause
 * state (Escape, pointer-lock loss, tab hide).
 *
 * Sibling modules are instantiated with their positional dependencies
 * followed by the shared `game` context as the LAST argument, so both
 * positional and context-style constructors work:
 *
 *   new World(scene, game)            terrain, trees, block storage
 *   new Player(camera, world, game)   input, physics, mouse look
 *   new Engine(game)                  shared services hub (built last, so the
 *                                     context is complete when it runs)
 *   new UI(uiRoot, game)              hotbar, crosshair, FPS, pause menu
 *
 * Optional hooks main calls (all failure-isolated, errors logged once):
 *   module.update(dt, game)   — every fixed step (UI: every frame)
 *   ui.setPaused(bool, game)  — on pause transitions
 *   ui.setFps(fps, game)      — roughly twice a second
 *   ui.resize(w, h, game)     — on window resize
 * ========================================================================== */

(function () {
  'use strict';

  // ------------------------------------------------------------------ tuning
  const STEP = 1 / 60;         // fixed simulation step (seconds)
  const MAX_STEPS = 5;         // catch-up steps per frame (spiral-of-death guard)
  const SKY_COLOR = 0x87ceeb;  // sky and fog colour
  const FAR_PLANE = 400;       // camera far plane
  const MAX_PIXEL_RATIO = 2;

  class Game {
    constructor() {
      if (Game.instance) return Game.instance; // never boot twice
      Game.instance = this;

      if (typeof THREE === 'undefined') {
        this._fail('three.js could not be loaded — check the CDN <script> tag.');
        return;
      }

      // ---- run state -------------------------------------------------------
      this.paused = false;        // shared pause flag (read by UI/Player)
      this.time = 0;              // simulated seconds
      this.fps = 0;               // rolling frame rate
      this._acc = 0;              // fixed-step accumulator
      this._last = 0;             // previous frame timestamp
      this._frames = 0;           // frames since the last FPS sample
      this._fpsClock = 0;
      this._running = false;
      this._rafId = 0;
      this._reported = new Set(); // keys of already-logged failures

      // ---- DOM -------------------------------------------------------------
      this.canvas = Game.element('game-canvas', 'canvas');
      this.uiRoot = Game.element('ui-root', 'div');

      // ---- renderer --------------------------------------------------------
      this.renderer = new THREE.WebGLRenderer({
        canvas: this.canvas,
        antialias: true,
        powerPreference: 'high-performance'
      });
      this._applyColorSpace();

      // ---- scene / camera / fog -------------------------------------------
      this.scene = new THREE.Scene();
      this.scene.background = new THREE.Color(SKY_COLOR);
      this.scene.fog = new THREE.Fog(SKY_COLOR, 40, 95);

      this.camera = new THREE.PerspectiveCamera(75, 1, 0.1, FAR_PLANE);
      this.camera.rotation.order = 'YXZ';    // FPS-style yaw/pitch
      this.camera.position.set(12, 48, 12);  // fallback viewpoint; Player takes over
      this.camera.lookAt(0, 20, 0);

      // ---- simple directional lighting --------------------------------------
      // three >= r155 evaluates light intensities in physical units, which read
      // roughly PI times darker than legacy numbers, so scale for whichever
      // build the page actually pulled in.
      const modern = (parseInt(THREE.REVISION, 10) || 0) >= 155;
      const sun = new THREE.DirectionalLight(0xfff3dd, modern ? 2.3 : 0.8);
      sun.position.set(60, 110, 40);
      this.scene.add(sun);
      this.scene.add(new THREE.HemisphereLight(0xcfe5ff, 0x7a6b52, modern ? 1.2 : 0.45));

      // ---- subsystems -------------------------------------------------------
      this.world = this._spawn('World', window.World, this.scene);
      this.player = this._spawn('Player', window.Player, this.camera, this.world);
      this.engine = this._spawn('Engine', window.Engine); // last: full context ready
      this.ui = this._spawn('UI', window.UI, this.uiRoot);

      this._tuneFog();
      this._buildFallbackHud(); // only used when the UI module is absent
      this._bindEvents();
    }

    // ------------------------------------------------------------ main loop
    start() {
      if (this._running || !this.renderer) return this;
      this._running = true;
      this._last = performance.now();
      this._acc = 0;
      const frame = (now) => {
        if (!this._running) return;
        this._rafId = requestAnimationFrame(frame); // queue first so one bad
        this._frame(now);                           // frame never kills the loop
      };
      this._rafId = requestAnimationFrame(frame);
      return this;
    }

    stop() {
      this._running = false;
      cancelAnimationFrame(this._rafId);
      return this;
    }

    _frame(now) {
      const dt = Math.min(Math.max((now - this._last) / 1000, 0), 0.25);
      this._last = now;

      if (!this.paused) {
        this._acc += dt;
        let steps = 0;
        while (this._acc >= STEP && steps < MAX_STEPS) {
          this._update(STEP);
          this._acc -= STEP;
          steps += 1;
        }
        if (steps === MAX_STEPS) this._acc = 0; // shed unpayable backlog
      }

      this._countFps(dt);
      this._call(this.ui, 'update', dt, this); // HUD refreshes every frame, even paused
      this._render();
    }

    _update(dt) {
      this.time += dt;
      this._call(this.engine, 'update', dt, this); // input / interaction pre-pass
      this._call(this.player, 'update', dt, this); // controls + AABB physics
      this._call(this.world, 'update', dt, this);  // chunk streaming / remeshing
    }

    _render() {
      try {
        this.renderer.render(this.scene, this.camera);
      } catch (err) {
        this._once('renderer', () => console.error('[Game] rendering failed:', err));
      }
    }

    _countFps(dt) {
      this._frames += 1;
      this._fpsClock += dt;
      if (this._fpsClock < 0.5) return;
      this.fps = Math.max(1, Math.round(this._frames / this._fpsClock));
      this._frames = 0;
      this._fpsClock = 0;
      this._call(this.ui, 'setFps', this.fps, this);
      if (this._fpsEl) this._fpsEl.textContent = this.fps + ' FPS';
    }

    // ------------------------------------------------------------------ pause
    togglePause() {
      this.setPaused(!this.paused);
    }

    setPaused(paused) {
      paused = !!paused;
      if (this.paused === paused) return;
      this.paused = paused;

      if (paused) {
        if (document.pointerLockElement) {
          try { document.exitPointerLock(); } catch (e) { /* ignore */ }
        }
      } else {
        this._last = performance.now(); // never integrate the pause gap
        this._acc = 0;
        this._requestLock();
      }

      this._call(this.ui, 'setPaused', paused, this);
      if (this._pauseEl) this._pauseEl.style.display = paused ? 'flex' : 'none';
    }

    _requestLock() {
      if (!this.canvas || document.pointerLockElement) return;
      try {
        const request = this.canvas.requestPointerLock();
        if (request && typeof request.catch === 'function') request.catch(() => {});
      } catch (e) {
        // Browsers refuse to re-lock for a moment after an Escape exit.
      }
    }

    _onLockChange() {
      const locked = !!document.pointerLockElement;
      if (this._hintEl && locked) this._hintEl.style.display = 'none';
      if (locked) {
        if (this.paused) this.setPaused(false); // lock (re)gained -> play
      } else if (!this.paused) {
        this.setPaused(true); // Escape or focus loss ended the lock -> pause
      }
    }

    // ----------------------------------------------------------------- events
    _bindEvents() {
      window.addEventListener('resize', () => this._onResize());

      document.addEventListener('pointerlockchange', () => this._onLockChange());
      document.addEventListener('pointerlockerror', () => {
        console.warn('[Game] pointer lock was refused by the browser.');
      });
      document.addEventListener('visibilitychange', () => {
        if (document.hidden) this.setPaused(true); // don't simulate a hidden tab
      });
      document.addEventListener('keydown', (e) => {
        if (e.code === 'Escape' && !e.repeat) {
          e.preventDefault();
          this.togglePause();
        }
      });

      // Clicking the canvas (re-)engages pointer lock; clicking a pause
      // overlay resumes the game.
      this.canvas.addEventListener('click', () => this._requestLock());
      this.uiRoot.addEventListener('click', () => {
        if (this.paused) this._requestLock();
      });

      // Right-click places blocks — never let the OS context menu steal it.
      this.canvas.addEventListener('contextmenu', (e) => e.preventDefault());

      this.canvas.addEventListener('webglcontextlost', (e) => {
        e.preventDefault();
        this.setPaused(true);
      });

      this._onResize(); // size everything before the first frame
    }

    _onResize() {
      const w = Math.max(window.innerWidth, 1);
      const h = Math.max(window.innerHeight, 1);
      this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, MAX_PIXEL_RATIO));
      this.renderer.setSize(w, h);
      this.camera.aspect = w / h;
      this.camera.updateProjectionMatrix();
      this._call(this.ui, 'resize', w, h, this);
    }

    // ---------------------------------------------------------------- helpers
    /** Construct a sibling module; the game context is always the last argument. */
    _spawn(name, Ctor) {
      const deps = Array.prototype.slice.call(arguments, 2);
      if (typeof Ctor !== 'function') {
        console.warn('[Game] ' + name + ' class is missing — running without it.');
        return null;
      }
      try {
        return new (Function.prototype.bind.apply(Ctor, [null].concat(deps, [this])))();
      } catch (err) {
        console.warn('[Game] failed to construct ' + name + ':', err);
        return null;
      }
    }

    /** Invoke an optional hook, isolating and de-duplicating failures. */
    _call(target, method) {
      if (!target || typeof target[method] !== 'function') return undefined;
      const args = Array.prototype.slice.call(arguments, 2);
      try {
        return target[method].apply(target, args);
      } catch (err) {
        const owner = (target.constructor && target.constructor.name) || 'module';
        this._once(owner + '.' + method, () =>
          console.error('[Game] ' + owner + '.' + method + ' keeps failing (reported once):', err));
      }
      return undefined;
    }

    _once(key, report) {
      if (this._reported.has(key)) return;
      this._reported.add(key);
      report();
    }

    _applyColorSpace() {
      const r = this.renderer;
      if (THREE.SRGBColorSpace && 'outputColorSpace' in r) {
        r.outputColorSpace = THREE.SRGBColorSpace; // three >= r152
      } else if (THREE.sRGBEncoding && 'outputEncoding' in r) {
        r.outputEncoding = THREE.sRGBEncoding;     // older builds
      }
    }

    /** If the world advertises its render distance, melt the terrain edge
     *  into the fog instead of showing a hard cut-off. */
    _tuneFog() {
      const fog = this.scene && this.scene.fog;
      const rd = this.world && this.world.renderDistance;
      if (!fog || typeof rd !== 'number' || rd <= 0) return;
      const reach = rd <= 32 ? rd * 16 : rd; // chunk count vs. block count
      fog.near = Math.max(16, reach * 0.5);
      fog.far = Math.max(48, reach * 0.98);
    }

    /** Minimal crosshair + FPS + pause screen, used only if UI is missing. */
    _buildFallbackHud() {
      if (this.ui) return; // the UI module owns the real HUD
      console.warn('[Game] UI class is missing — using the built-in minimal HUD.');
      const base = 'position:fixed;z-index:10;pointer-events:none;color:#fff;' +
                   'text-shadow:0 1px 2px rgba(0,0,0,.8);';
      this._fpsEl = document.createElement('div');
      this._fpsEl.style.cssText = base + 'top:8px;left:10px;font:13px monospace;';
      this._fpsEl.textContent = '0 FPS';

      const crosshair = document.createElement('div');
      crosshair.style.cssText = base + 'left:50%;top:50%;width:16px;height:16px;' +
        'transform:translate(-50%,-50%);background:' +
        'linear-gradient(#fff,#fff) center/2px 100% no-repeat,' +
        'linear-gradient(#fff,#fff) center/100% 2px no-repeat;';

      this._hintEl = document.createElement('div');
      this._hintEl.style.cssText = base + 'left:50%;top:62%;transform:translateX(-50%);font:14px sans-serif;';
      this._hintEl.textContent = 'Click to capture the mouse';

      this._pauseEl = document.createElement('div');
      this._pauseEl.style.cssText = 'position:fixed;inset:0;z-index:11;display:none;' +
        'align-items:center;justify-content:center;font:bold 22px sans-serif;' +
        'color:#fff;background:rgba(10,14,18,.55);';
      this._pauseEl.textContent = 'PAUSED — click or press Escape to resume';

      this.uiRoot.appendChild(this._fpsEl);
      this.uiRoot.appendChild(crosshair);
      this.uiRoot.appendChild(this._hintEl);
      this.uiRoot.appendChild(this._pauseEl);
    }

    _fail(message) {
      console.error('[Game] ' + message);
      const box = document.createElement('div');
      box.style.cssText = 'position:fixed;inset:0;display:flex;align-items:center;' +
        'justify-content:center;padding:24px;text-align:center;font:15px sans-serif;' +
        'color:#fff;background:#16191d;';
      box.textContent = message;
      document.body.appendChild(box);
    }

    /** Grab a page element, creating a sensible fallback if it is absent. */
    static element(id, tag) {
      let el = document.getElementById(id);
      if (!el) {
        el = document.createElement(tag);
        el.id = id;
        if (tag === 'canvas') el.style.cssText = 'position:fixed;inset:0;display:block;';
        document.body.appendChild(el);
        console.warn('[Game] #' + id + ' was missing from the page — created a fallback.');
      }
      return el;
    }
  }

  // ---- contract -------------------------------------------------------------
  window.Game = Game;

  // ---- boot -------------------------------------------------------------------
  function boot() {
    try {
      new Game().start();
    } catch (err) {
      console.error('[Game] fatal bootstrap error:', err);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }
})();
