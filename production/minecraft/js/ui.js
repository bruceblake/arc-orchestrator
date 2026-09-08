/* -------------------------------------------------------------
   UI – heads‑up‑display for the voxel sandbox
   -------------------------------------------------------------
   Provides:
     • Cross‑hair (center of screen)
     • Hot‑bar (9 block slots, selectable with 1‑9)
     • FPS counter (top‑left)
     • Pause / Help overlay (shown on Escape)
   -------------------------------------------------------------
   Global contract: window.UI
   ------------------------------------------------------------- */

(function () {
    // -----------------------------------------------------------------
    // Helper shortcuts – the other modules are already present in globals.
    // -----------------------------------------------------------------
    const { Engine } = window.engine;               // engine tick information
    const { BLOCKS, World } = window.world;        // block catalogue
    const { Player } = window.player;              // current player object
    const { Game } = window.main;                  // Game.togglePause(), Game.isPaused
    const { canvas, uiRoot } = window.html;        // <canvas> + UI container

    // -----------------------------------------------------------------
    // UI object – singleton that is exported as window.UI
    // -----------------------------------------------------------------
    const UI = {
        // DOM elements (filled by init())
        _crosshair: null,
        _hotbar: null,
        _hotSlots: [],           // array of 9 slot divs
        _fpsCounter: null,
        _pauseOverlay: null,
        _helpText: null,

        // Runtime data
        _lastFpsUpdate: 0,
        _fps: 0,
        _selectedSlot: 0,       // 0‑based index (0 = slot 1)

        // -----------------------------------------------------------------
        // Initialise everything – called once at load time.
        // -----------------------------------------------------------------
        init() {
            this._createStyleSheet();
            this._createCrosshair();
            this._createHotbar();
            this._createFpsCounter();
            this._createPauseOverlay();
            this._registerInput();
            this.selectSlot(0); // default to first block
        },

        // -----------------------------------------------------------------
        // Update – called from the main game loop each frame.
        // delta is the fixed time‑step in seconds (Engine.dt)
        // -----------------------------------------------------------------
        update(delta) {
            // FPS counter – update roughly every 0.25 s to avoid flicker
            this._lastFpsUpdate += delta;
            if (this._lastFpsUpdate >= 0.25) {
                this._fps = Math.round(1 / delta);
                this._fpsCounter.textContent = `${this._fps} FPS`;
                this._lastFpsUpdate = 0;
            }
        },

        // -----------------------------------------------------------------
        // Switch the selected hot‑bar slot (0‑8).  Updates UI highlight and
        // tells the Player which block type is active.
        // -----------------------------------------------------------------
        selectSlot(index) {
            if (index < 0 || index > 8) return;
            this._selectedSlot = index;

            // UI highlight
            this._hotSlots.forEach((el, i) => {
                if (i === index) {
                    el.classList.add('selected');
                } else {
                    el.classList.remove('selected');
                }
            });

            // Tell the player which block texture to place
            const block = BLOCKS[index] || BLOCKS[0];
            if (Player && typeof Player.setSelectedBlock === 'function') {
                Player.setSelectedBlock(block);
            }
        },

        // -----------------------------------------------------------------
        // Show / hide the pause overlay.
        // -----------------------------------------------------------------
        setPaused(paused) {
            this._pauseOverlay.style.display = paused ? 'flex' : 'none';
        },

        // -----------------------------------------------------------------
        // Internal: create a small style‑sheet for the UI.
        // -----------------------------------------------------------------
        _createStyleSheet() {
            const style = document.createElement('style');
            style.textContent = `
                /* UI container – already positioned absolute via html */
                #ui-root {
                    position: absolute;
                    top: 0; left: 0;
                    width: 100%; height: 100%;
                    pointer-events: none;   /* allow canvas interaction */
                    font-family: sans-serif;
                }
                .crosshair {
                    position: absolute;
                    top: 50%; left: 50%;
                    width: 20px; height: 20px;
                    margin: -10px 0 0 -10px;
                    background: transparent;
                    pointer-events: none;
                }
                .crosshair:before, .crosshair:after {
                    content: "";
                    position: absolute;
                    background: #fff;
                }
                .crosshair:before {
                    left: 50%; top: 0;
                    width: 2px; height: 100%;
                    transform: translateX(-50%);
                }
                .crosshair:after {
                    top: 50%; left: 0;
                    width: 100%; height: 2px;
                    transform: translateY(-50%);
                }
                .hotbar {
                    position: absolute;
                    bottom: 20px; left: 50%;
                    transform: translateX(-50%);
                    display: flex;
                    background: rgba(0,0,0,0.4);
                    padding: 4px;
                    border-radius: 6px;
                    pointer-events: none;
                }
                .hot-slot {
                    width: 40px; height: 40px;
                    margin: 0 2px;
                    background-size: cover;
                    background-position: center;
                    border: 2px solid transparent;
                }
                .hot-slot.selected {
                    border-color: #ff0;
                }
                .fps-counter {
                    position: absolute;
                    top: 8px; left: 8px;
                    color: #0f0;
                    background: rgba(0,0,0,0.5);
                    padding: 2px 5px;
                    border-radius: 3px;
                    font-size: 13px;
                    pointer-events: none;
                }
                .pause-overlay {
                    position: absolute;
                    top: 0; left: 0;
                    width: 100%; height: 100%;
                    background: rgba(0,0,0,0.7);
                    color: #fff;
                    display: none;
                    align-items: center;
                    justify-content: center;
                    flex-direction: column;
                    text-align: center;
                    font-size: 20px;
                }
                .pause-overlay h1 { margin: 0 0 10px; font-size: 2em; }
                .pause-overlay p { margin: 5px 0; }
            `;
            document.head.appendChild(style);
        },

        // -----------------------------------------------------------------
        // Internal: create and inject the cross‑hair element.
        // -----------------------------------------------------------------
        _createCrosshair() {
            const el = document.createElement('div');
            el.className = 'crosshair';
            uiRoot.appendChild(el);
            this._crosshair = el;
        },

        // -----------------------------------------------------------------
        // Internal: build the hot‑bar with 9 slots.
        // -----------------------------------------------------------------
        _createHotbar() {
            const bar = document.createElement('div');
            bar.className = 'hotbar';
            uiRoot.appendChild(bar);
            this._hotbar = bar;

            for (let i = 0; i < 9; i++) {
                const slot = document.createElement('div');
                slot.className = 'hot-slot';
                const block = BLOCKS[i] || BLOCKS[0];
                // Assume each block has a .texture property that is a URL.
                if (block && block.texture) slot.style.backgroundImage = `url(${block.texture})`;
                // Add the slot number for debugging / visibility
                const label = document.createElement('div');
                label.style.position = 'absolute';
                label.style.bottom = '2px';
                label.style.right = '2px';
                label.style.color = '#fff';
                label.style.fontSize = '10px';
                label.textContent = i + 1;
                slot.appendChild(label);
                bar.appendChild(slot);
                this._hotSlots.push(slot);
            }
        },

        // -----------------------------------------------------------------
        // Internal: create FPS counter.
        // -----------------------------------------------------------------
        _createFpsCounter() {
            const fps = document.createElement('div');
            fps.className = 'fps-counter';
            fps.textContent = '0 FPS';
            uiRoot.appendChild(fps);
            this._fpsCounter = fps;
        },

        // -----------------------------------------------------------------
        // Internal: create pause/help overlay.
        // -----------------------------------------------------------------
        _createPauseOverlay() {
            const overlay = document.createElement('div');
            overlay.className = 'pause-overlay';
            overlay.innerHTML = `
                <h1>Game Paused</h1>
                <p>Press <b>Esc</b> again to resume.</p>
                <p>Movement: <b>W A S D</b> Jump: <b>Space</b> Sprint: <b>Shift</b></p>
                <p>Break block: <b>Left Click</b> Place block: <b>Right Click</b></p>
                <p>Hot‑bar: <b>1‑9</b> Select block type to place.</p>
                <p>Enjoy building!</p>
            `;
            uiRoot.appendChild(overlay);
            this._pauseOverlay = overlay;
        },

        // -----------------------------------------------------------------
        // Internal: key handling for hot‑bar selection and pause.
        // -----------------------------------------------------------------
        _registerInput() {
            // We need the events on the whole document; UI should not block.
            document.addEventListener('keydown', (e) => {
                // Number keys 1‑9 (both top row and numpad)
                if (/^Digit[1-9]$/.test(e.code)) {
                    const slot = parseInt(e.key, 10) - 1;
                    UI.selectSlot(slot);
                } else if (/^Numpad[1-9]$/.test(e.code)) {
                    const slot = parseInt(e.code.slice(-1), 10) - 1;
                    UI.selectSlot(slot);
                } else if (e.code === 'Escape') {
                    // Toggle pause through the Game object
                    if (Game && typeof Game.togglePause === 'function') {
                        Game.togglePause();
                        UI.setPaused(Game.isPaused);
                    }
                }
            });
        }
    };

    // -----------------------------------------------------------------
    // Expose the UI singleton
    // -----------------------------------------------------------------
    window.UI = UI;

    // -----------------------------------------------------------------
    // Auto‑initialise once the DOM is ready (scripts are loaded after the
    // html elements, but we guard against race conditions)
    // -----------------------------------------------------------------
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => UI.init());
    } else {
        UI.init();
    }
})();
