// js/player.js
class Player {
  constructor(world, camera, ui, engine) {
    this.world = world || null;
    this.camera = camera || null;
    this.ui = ui || null;
    this.engine = engine || null;
    this.canvas = (engine && engine.renderer && engine.renderer.domElement) || document.getElementById('game-canvas');

    this.position = new THREE.Vector3(0.5, 40, 0.5);
    this.velocity = new THREE.Vector3();
    this.onGround = false;
    this.paused = false;
    this.controlsEnabled = false;
    this.yaw = 0;
    this.pitch = 0;
    this.keys = {};

    // Spawn position if world provides one
    if (this.world) {
      if (typeof this.world.getSpawnPos === 'function') this.position.copy(this.world.getSpawnPos());
      else if (this.world.spawnPoint) this.position.copy(this.world.spawnPoint);
      else if (typeof this.world.getHeight === 'function') {
        this.position.set(0.5, this.world.getHeight(0, 0) + 1, 0.5);
      }
    }

    // Hotbar / block selection
    this.hotbar = [];
    if (window.BLOCKS && typeof window.BLOCKS === 'object') {
      const ids = Object.keys(window.BLOCKS)
        .map(Number)
        .filter(id => id > 0 && !(window.BLOCKS[id] && window.BLOCKS[id].solid === false));
      this.hotbar = ids.slice(0, 9).length ? ids.slice(0, 9) : [1, 2, 3, 4, 5];
    } else {
      this.hotbar = [1, 2, 3, 4, 5];
    }
    this.selectedBlock = this.hotbar[0];

    this.EYE = 1.6;
    this.WIDTH = 0.6;
    this.HEIGHT = 1.8;
    this.GRAVITY = 28;
    this.JUMP_VELOCITY = 8.5;
    this.WALK_SPEED = 4.5;
    this.SPRINT_SPEED = 7.0;
    this.EPS = 0.001;

    this.minX = this.minY = this.minZ = 0;
    this.maxX = this.maxY = this.maxZ = 0;
    this.updateAABB();

    this._bindInput();
    if (this.ui && this.ui.setHotbar) this.ui.setHotbar(this.hotbar);
    if (this.ui && this.ui.setSelectedBlock) this.ui.setSelectedBlock(this.selectedBlock);
  }

  updateAABB() {
    this.minX = this.position.x - this.WIDTH / 2;
    this.maxX = this.position.x + this.WIDTH / 2;
    this.minY = this.position.y;
    this.maxY = this.position.y + this.HEIGHT;
    this.minZ = this.position.z - this.WIDTH / 2;
    this.maxZ = this.position.z + this.WIDTH / 2;
  }

  isSolid(x, y, z) {
    if (!this.world) return false;
    const block = this.world.getBlock(x, y, z);
    if (!block || block === 0) return false;
    const def = window.BLOCKS ? window.BLOCKS[block] : null;
    if (def && def.solid === false) return false;
    return true;
  }

  moveAxis(dx, dy, dz) {
    this.position.x += dx;
    this.position.y += dy;
    this.position.z += dz;
    this.updateAABB();

    let collided;
    do {
      collided = false;
      const bx0 = Math.floor(this.minX), bx1 = Math.floor(this.maxX);
      const by0 = Math.floor(this.minY), by1 = Math.floor(this.maxY);
      const bz0 = Math.floor(this.minZ), bz1 = Math.floor(this.maxZ);
      for (let bx = bx0; bx <= bx1 && !collided; bx++) {
        for (let by = by0; by <= by1 && !collided; by++) {
          for (let bz = bz0; bz <= bz1 && !collided; bz++) {
            if (this.isSolid(bx, by, bz)) {
              if (dx > 0) { this.position.x = bx - this.WIDTH / 2 - this.EPS; this.velocity.x = 0; }
              else if (dx < 0) { this.position.x = bx + 1 + this.WIDTH / 2 + this.EPS; this.velocity.x = 0; }
              if (dy > 0) { this.position.y = by - this.HEIGHT - this.EPS; this.velocity.y = 0; }
              else if (dy < 0) { this.position.y = by + 1 + this.EPS; this.velocity.y = 0; this.onGround = true; }
              if (dz > 0) { this.position.z = bz - this.WIDTH / 2 - this.EPS; this.velocity.z = 0; }
              else if (dz < 0) { this.position.z = bz + 1 + this.WIDTH / 2 + this.EPS; this.velocity.z = 0; }
              this.updateAABB();
              collided = true;
            }
          }
        }
      }
    } while (collided);
  }

  raycast(maxDistance = 7) {
    if (!this.camera) return null;
    const dir = new THREE.Vector3();
    this.camera.getWorldDirection(dir);
    const origin = this.camera.position;
    let x = Math.floor(origin.x), y = Math.floor(origin.y), z = Math.floor(origin.z);

    const stepX = dir.x > 0 ? 1 : dir.x < 0 ? -1 : 0;
    const stepY = dir.y > 0 ? 1 : dir.y < 0 ? -1 : 0;
    const stepZ = dir.z > 0 ? 1 : dir.z < 0 ? -1 : 0;

    const tDeltaX = stepX === 0 ? Infinity : Math.abs(1 / dir.x);
    const tDeltaY = stepY === 0 ? Infinity : Math.abs(1 / dir.y);
    const tDeltaZ = stepZ === 0 ? Infinity : Math.abs(1 / dir.z);

    let tMaxX = stepX === 0 ? Infinity : ((stepX > 0 ? (x + 1 - origin.x) : (origin.x - x)) * Math.abs(1 / dir.x));
    let tMaxY = stepY === 0 ? Infinity : ((stepY > 0 ? (y + 1 - origin.y) : (origin.y - y)) * Math.abs(1 / dir.y));
    let tMaxZ = stepZ === 0 ? Infinity : ((stepZ > 0 ? (z + 1 - origin.z) : (origin.z - z)) * Math.abs(1 / dir.z));

    if (this.isSolid(x, y, z)) {
      return { x, y, z, normal: { x: 0, y: 0, z: 0 } };
    }

    for (let i = 0; i < maxDistance; i++) {
      let nx = 0, ny = 0, nz = 0;
      if (tMaxX < tMaxY && tMaxX < tMaxZ) {
        x += stepX; tMaxX += tDeltaX; nx = -stepX;
      } else if (tMaxY < tMaxZ) {
        y += stepY; tMaxY += tDeltaY; ny = -stepY;
      } else {
        z += stepZ; tMaxZ += tDeltaZ; nz = -stepZ;
      }
      if (this.isSolid(x, y, z)) {
        return { x, y, z, normal: { x: nx, y: ny, z: nz } };
      }
    }
    return null;
  }

  blockIntersectsPlayer(bx, by, bz) {
    this.updateAABB();
    return bx < this.maxX && bx + 1 > this.minX &&
           by < this.maxY && by + 1 > this.minY &&
           bz < this.maxZ && bz + 1 > this.minZ;
  }

  breakBlock() {
    if (!this.world) return;
    const hit = this.raycast();
    if (hit) this.world.setBlock(hit.x, hit.y, hit.z, 0);
  }

  placeBlock() {
    if (!this.world) return;
    const hit = this.raycast();
    if (!hit) return;
    const px = hit.x + hit.normal.x;
    const py = hit.y + hit.normal.y;
    const pz = hit.z + hit.normal.z;
    if (this.isSolid(px, py, pz)) return;
    if (this.blockIntersectsPlayer(px, py, pz)) return;
    this.world.setBlock(px, py, pz, this.selectedBlock);
  }

  selectHotbar(index) {
    const i = index - 1;
    if (i >= 0 && i < this.hotbar.length) {
      this.selectedBlock = this.hotbar[i];
      if (this.ui && this.ui.setSelectedBlock) this.ui.setSelectedBlock(this.selectedBlock);
    }
  }

  requestPointerLock() {
    if (this.canvas && this.canvas.requestPointerLock) {
      this.canvas.requestPointerLock();
    }
  }

  pause() {
    if (document.pointerLockElement) document.exitPointerLock();
  }

  resume() {
    if (!this.controlsEnabled) this.requestPointerLock();
  }

  _bindInput() {
    window.addEventListener('keydown', (e) => {
      if (!this.controlsEnabled || this.paused) return;
      this.keys[e.code] = true;
      if (e.code.startsWith('Digit')) {
        this.selectHotbar(parseInt(e.code.slice(5)));
      }
    });
    window.addEventListener('keyup', (e) => {
      this.keys[e.code] = false;
    });
    window.addEventListener('blur', () => { this.keys = {}; });

    if (this.canvas) {
      this.canvas.addEventListener('mousedown', (e) => {
        if (!this.controlsEnabled) {
          e.preventDefault();
          this.requestPointerLock();
          return;
        }
        if (e.button === 0) this.breakBlock();
        else if (e.button === 2) this.placeBlock();
      });
      this.canvas.addEventListener('contextmenu', (e) => e.preventDefault());
    }

    document.addEventListener('mousemove', (e) => {
      if (!this.controlsEnabled || this.paused) return;
      const sens = 0.002;
      this.yaw -= e.movementX * sens;
      this.pitch -= e.movementY * sens;
      const limit = Math.PI / 2 - 0.01;
      this.pitch = Math.max(-limit, Math.min(limit, this.pitch));
    });

    document.addEventListener('pointerlockchange', () => {
      if (document.pointerLockElement === this.canvas) {
        this.controlsEnabled = true;
        this.paused = false;
        this.keys = {};
        if (this.ui && this.ui.resume) this.ui.resume();
      } else {
        this.controlsEnabled = false;
        this.paused = true;
        this.keys = {};
        if (this.ui && this.ui.pause) this.ui.pause();
      }
    });
  }

  update(dt = 0.016) {
    if (!this.controlsEnabled || this.paused || !this.world) return;

    // Jump
    if (this.keys['Space'] && this.onGround) {
      this.velocity.y = this.JUMP_VELOCITY;
      this.onGround = false;
    }

    // Gravity
    this.velocity.y -= this.GRAVITY * dt;

    // Movement input
    const forward = (this.keys['KeyW'] ? 1 : 0) - (this.keys['KeyS'] ? 1 : 0);
    const strafe = (this.keys['KeyD'] ? 1 : 0) - (this.keys['KeyA'] ? 1 : 0);
    if (forward !== 0 || strafe !== 0) {
      const sprint = (this.keys['ShiftLeft'] || this.keys['ShiftRight']) && forward > 0;
      const speed = sprint ? this.SPRINT_SPEED : this.WALK_SPEED;
      const len = Math.hypot(forward, strafe);
      const f = forward / len;
      const s = strafe / len;
      const sinY = Math.sin(this.yaw), cosY = Math.cos(this.yaw);
      const dirX = f * -sinY + s * cosY;
      const dirZ = f * -cosY + s * -sinY;
      this.velocity.x = dirX * speed;
      this.velocity.z = dirZ * speed;
    } else {
      this.velocity.x = 0;
      this.velocity.z = 0;
    }

    // Move axes separately
    this.moveAxis(this.velocity.x * dt, 0, 0);
    this.moveAxis(0, 0, this.velocity.z * dt);
    this.onGround = false;
    this.moveAxis(0, this.velocity.y * dt, 0);

    // Update camera
    if (this.camera) {
      this.camera.rotation.order = 'YXZ';
      this.camera.rotation.y = this.yaw;
      this.camera.rotation.x = this.pitch;
      this.camera.position.set(this.position.x, this.position.y + this.EYE, this.position.z);
    }
  }
}

window.Player = Player;
