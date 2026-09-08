/**
 * world.js — voxel world module (globals: World, BLOCKS)
 * Chunk storage, block get/set, value-noise terrain with hills + trees,
 * and per-chunk meshing into THREE.BufferGeometry (vertex-colored faces).
 * Plain <script> module (no imports); requires global THREE loaded first.
 *
 * Public API used by other modules:
 *   BLOCKS.<NAME>            -> { id, name, solid, color|top|side|bottom, transparent? }
 *   BLOCKS.byId[id]          -> block def by numeric id (0 = air)
 *   new World(scene)         -> generates terrain + meshes, adds to scene
 *   world.getBlock(x,y,z)    -> block id (0 if air / out of bounds)
 *   world.setBlock(x,y,z,id) -> writes block, rebuilds affected chunk meshes
 *   world.isSolid(x,y,z)     -> boolean (safe with float coords)
 *   world.getTopY(x,z)       -> y of highest solid block (a.k.a. getHeight)
 *   world.getSpawn()         -> THREE.Vector3 standing spot near world center
 *   world.raycast(origin,dir,maxDist) -> { block, x,y,z, nx,ny,nz, place:{x,y,z}, distance } | null
 *   world.group              -> THREE.Group containing all chunk meshes
 */
(function () {
'use strict';

/* ----------------------------- blocks ----------------------------- */

const BLOCKS = {
  AIR:    { id: 0, name: 'Air',    solid: false },
  GRASS:  { id: 1, name: 'Grass',  solid: true,  top: 0x55a630, side: 0x4e8c2a, bottom: 0x7a5a34 },
  DIRT:   { id: 2, name: 'Dirt',   solid: true,  color: 0x7a5a34 },
  STONE:  { id: 3, name: 'Stone',  solid: true,  color: 0x8f8f8f },
  WOOD:   { id: 4, name: 'Wood',   solid: true,  side: 0x6e5232, top: 0xa08050, bottom: 0xa08050 },
  LEAVES: { id: 5, name: 'Leaves', solid: true,  color: 0x3f7d26 },
  SAND:   { id: 6, name: 'Sand',   solid: true,  color: 0xdbc98a },
  BRICK:  { id: 7, name: 'Brick',  solid: true,  color: 0xa24434 },
  PLANK:  { id: 8, name: 'Planks', solid: true,  color: 0xb3905a },
  GLASS:  { id: 9, name: 'Glass',  solid: true,  color: 0xa8d8e8, transparent: true },
};
BLOCKS.byId = Object.keys(BLOCKS)
  .map(function (k) { return BLOCKS[k]; })
  .sort(function (a, b) { return a.id - b.id; });

function faceColor(def, part) {
  if (def[part] !== undefined) return def[part];
  if (def.side !== undefined)  return def.side;
  return def.color !== undefined ? def.color : 0xffffff;
}

/* ----------------------------- noise ------------------------------ */

function hash2(x, z, seed) {
  let h = Math.imul(x | 0, 0x27d4eb2d) ^ Math.imul(z | 0, 0x165667b1) ^ Math.imul(seed | 0, 0x9e3779b9);
  h = Math.imul(h ^ (h >>> 15), 0x85ebca6b);
  h ^= h >>> 13;
  return (h >>> 0) / 4294967296;
}
function smooth(t) { return t * t * (3 - 2 * t); }

function valueNoise(x, z, seed) {
  const xi = Math.floor(x), zi = Math.floor(z);
  const u = smooth(x - xi), v = smooth(z - zi);
  const a = hash2(xi, zi, seed),     b = hash2(xi + 1, zi, seed);
  const c = hash2(xi, zi + 1, seed), d = hash2(xi + 1, zi + 1, seed);
  return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
}

function fbm(x, z, seed, octaves) {
  let sum = 0, amp = 1, freq = 1, norm = 0;
  for (let i = 0; i < octaves; i++) {
    sum  += amp * valueNoise(x * freq, z * freq, seed + i * 131);
    norm += amp;
    amp  *= 0.5;
    freq *= 2.17;
  }
  return sum / norm;
}

/* ----------------------------- faces ------------------------------ */

const FACES = [
  { n: [ 1, 0, 0], part: 'side',   v: [[1,0,0],[1,1,0],[1,1,1],[1,0,1]] },
  { n: [-1, 0, 0], part: 'side',   v: [[0,0,1],[0,1,1],[0,1,0],[0,0,0]] },
  { n: [ 0, 1, 0], part: 'top',    v: [[0,1,1],[1,1,1],[1,1,0],[0,1,0]] },
  { n: [ 0,-1, 0], part: 'bottom', v: [[0,0,0],[1,0,0],[1,0,1],[0,0,1]] },
  { n: [ 0, 0, 1], part: 'side',   v: [[1,0,1],[1,1,1],[0,1,1],[0,0,1]] },
  { n: [ 0, 0,-1], part: 'side',   v: [[0,0,0],[0,1,0],[1,1,0],[1,0,0]] },
];

/* ----------------------------- world ------------------------------ */

class World {
  constructor(scene) {
    this.scene    = scene || null;
    this.chunks   = new Map(); // "cx,cz" -> { cx, cz, data:Uint8Array, mesh }
    this.group    = new THREE.Group();
    this.group.name = 'world';
    this.material = World.material;
    if (scene) scene.add(this.group);
    this.generate();
  }

  attachScene(scene) {
    this.scene = scene;
    if (scene && this.group.parent !== scene) scene.add(this.group);
  }

  static index(lx, y, lz) {
    const CS = World.CHUNK_SIZE;
    return (y * CS + lz) * CS + lx;
  }

  /* ------- generation ------- */

  generate() {
    const R = World.CHUNKS_WIDE / 2;
    for (let cx = -R; cx < R; cx++)
      for (let cz = -R; cz < R; cz++)
        this.generateChunk(cx, cz);
    this.plantTrees();
    this.chunks.forEach((chunk) => this.buildMesh(chunk));
  }

  terrainHeight(wx, wz) {
    const hills  = fbm(wx * 0.018, wz * 0.018, World.SEED, 4);
    const detail = fbm(wx * 0.09,  wz * 0.09,  World.SEED + 7, 2);
    return Math.max(2, Math.floor(6 + hills * 24 + detail * 4));
  }

  generateChunk(cx, cz) {
    const CS = World.CHUNK_SIZE, H = World.HEIGHT;
    const data  = new Uint8Array(CS * CS * H);
    const chunk = { cx: cx, cz: cz, data: data, mesh: null };
    this.chunks.set(cx + ',' + cz, chunk);

    for (let lx = 0; lx < CS; lx++) {
      for (let lz = 0; lz < CS; lz++) {
        const wx = cx * CS + lx, wz = cz * CS + lz;
        const h  = Math.min(this.terrainHeight(wx, wz), H - 1);
        const sandy = h <= World.SAND_LEVEL;
        for (let y = 0; y <= h; y++) {
          let id;
          if (y === 0)          id = BLOCKS.STONE.id;
          else if (y === h)     id = sandy ? BLOCKS.SAND.id : BLOCKS.GRASS.id;
          else if (y > h - 4)   id = h <= World.SAND_LEVEL + 1 ? BLOCKS.SAND.id : BLOCKS.DIRT.id;
          else                  id = BLOCKS.STONE.id;
          data[World.index(lx, y, lz)] = id;
        }
      }
    }
  }

  plantTrees() {
    const half = (World.CHUNKS_WIDE * World.CHUNK_SIZE) / 2;
    for (let x = -half + 2; x < half - 2; x++) {
      for (let z = -half + 2; z < half - 2; z++) {
        if (hash2(x, z, World.SEED + 9001) > World.TREE_DENSITY) continue;
        const h = this.terrainHeight(x, z);
        if (h <= World.SAND_LEVEL + 1) continue;
        this.growTree(x, h + 1, z);
      }
    }
  }

  growTree(x, y, z) {
    const trunk = 4 + Math.floor(hash2(x, z, World.SEED + 55) * 2); // 4–5
    const top   = y + trunk - 1;
    for (let ly = top - 2; ly <= top + 1; ly++) {
      const r = ly <= top - 1 ? 2 : 1;
      for (let dx = -r; dx <= r; dx++) {
        for (let dz = -r; dz <= r; dz++) {
          if (r === 2 && Math.abs(dx) === 2 && Math.abs(dz) === 2 &&
              hash2(x + dx, z + dz, ly) < 0.5) continue; // ragged corners
          if (this.getBlock(x + dx, ly, z + dz) === BLOCKS.AIR.id)
            this.setRaw(x + dx, ly, z + dz, BLOCKS.LEAVES.id);
        }
      }
    }
    for (let i = 0; i < trunk; i++) {
      const cur = this.getBlock(x, y + i, z);
      if (cur === BLOCKS.AIR.id || cur === BLOCKS.LEAVES.id)
        this.setRaw(x, y + i, z, BLOCKS.WOOD.id);
    }
  }

  /* ------- block access ------- */

  getBlock(x, y, z) {
    x = Math.floor(x); y = Math.floor(y); z = Math.floor(z);
    if (y < 0 || y >= World.HEIGHT) return 0;
    const CS = World.CHUNK_SIZE;
    const cx = Math.floor(x / CS), cz = Math.floor(z / CS);
    const chunk = this.chunks.get(cx + ',' + cz);
    if (!chunk) return 0;
    return chunk.data[World.index(x - cx * CS, y, z - cz * CS)];
  }

  setRaw(x, y, z, id) { // writes storage only, no remesh
    x = Math.floor(x); y = Math.floor(y); z = Math.floor(z);
    if (y < 0 || y >= World.HEIGHT) return false;
    const CS = World.CHUNK_SIZE;
    const cx = Math.floor(x / CS), cz = Math.floor(z / CS);
    const chunk = this.chunks.get(cx + ',' + cz);
    if (!chunk) return false;
    chunk.data[World.index(x - cx * CS, y, z - cz * CS)] = id;
    return true;
  }

  setBlock(x, y, z, id) {
    if (!this.setRaw(x, y, z, id)) return false;
    const CS = World.CHUNK_SIZE;
    x = Math.floor(x); z = Math.floor(z);
    const cx = Math.floor(x / CS), cz = Math.floor(z / CS);
    const lx = x - cx * CS,        lz = z - cz * CS;
    this.rebuild(cx, cz);
    if (lx === 0)      this.rebuild(cx - 1, cz);
    if (lx === CS - 1) this.rebuild(cx + 1, cz);
    if (lz === 0)      this.rebuild(cx, cz - 1);
    if (lz === CS - 1) this.rebuild(cx, cz + 1);
    return true;
  }

  isSolid(x, y, z) {
    return BLOCKS.byId[this.getBlock(x, y, z)].solid;
  }

  getTopY(x, z) {
    for (let y = World.HEIGHT - 1; y >= 0; y--)
      if (this.isSolid(x, y, z)) return y;
    return 0;
  }
  getHeight(x, z) { return this.getTopY(x, z); } // alias

  getSpawn() {
    const y = this.getTopY(0, 0) + 1;
    return new THREE.Vector3(0.5, y + 0.01, 0.5);
  }

  /* ------- meshing ------- */

  rebuild(cx, cz) {
    const chunk = this.chunks.get(cx + ',' + cz);
    if (chunk) this.buildMesh(chunk);
  }

  buildMesh(chunk) {
    const CS = World.CHUNK_SIZE, H = World.HEIGHT;
    const positions = [], normals = [], colors = [], indices = [];
    const color = new THREE.Color();
    const baseX = chunk.cx * CS, baseZ = chunk.cz * CS;

    for (let y = 0; y < H; y++) {
      for (let lz = 0; lz < CS; lz++) {
        for (let lx = 0; lx < CS; lx++) {
          const id = chunk.data[World.index(lx, y, lz)];
          if (id === 0) continue;
          const def = BLOCKS.byId[id];
          const wx = baseX + lx, wz = baseZ + lz;

          for (let f = 0; f < 6; f++) {
            const face = FACES[f];
            const nid  = this.getBlock(wx + face.n[0], y + face.n[1], wz + face.n[2]);
            if (nid !== 0) {
              const ndef = BLOCKS.byId[nid];
              if (ndef.solid && !ndef.transparent) continue;  // hidden by opaque neighbor
              if (ndef.id === id && def.transparent) continue; // glass-to-glass
            }
            const shade = (0.9 + 0.1 * hash2(wx * 7 + f, wz * 7 + y, id)) *
                          (0.7 + 0.3 * Math.min(1, (y + 8) / 40));
            color.setHex(faceColor(def, face.part)).multiplyScalar(shade);

            const base = positions.length / 3;
            for (let c = 0; c < 4; c++) {
              positions.push(wx + face.v[c][0], y + face.v[c][1], wz + face.v[c][2]);
              normals.push(face.n[0], face.n[1], face.n[2]);
              colors.push(color.r, color.g, color.b);
            }
            indices.push(base, base + 1, base + 2, base, base + 2, base + 3);
          }
        }
      }
    }

    if (chunk.mesh) {
      this.group.remove(chunk.mesh);
      chunk.mesh.geometry.dispose();
      chunk.mesh = null;
    }
    if (indices.length === 0) return;

    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
    geo.setAttribute('normal',   new THREE.Float32BufferAttribute(normals, 3));
    geo.setAttribute('color',    new THREE.Float32BufferAttribute(colors, 3));
    geo.setIndex(indices);
    geo.computeBoundingSphere();

    const mesh = new THREE.Mesh(geo, this.material);
    mesh.matrixAutoUpdate = false;
    this.group.add(mesh);
    chunk.mesh = mesh;
  }

  /* ------- voxel raycast (break / place) ------- */

  raycast(origin, dir, maxDist) {
    let x = Math.floor(origin.x), y = Math.floor(origin.y), z = Math.floor(origin.z);
    const stepX = dir.x > 0 ? 1 : -1, stepY = dir.y > 0 ? 1 : -1, stepZ = dir.z > 0 ? 1 : -1;
    const tdx = dir.x !== 0 ? Math.abs(1 / dir.x) : Infinity;
    const tdy = dir.y !== 0 ? Math.abs(1 / dir.y) : Infinity;
    const tdz = dir.z !== 0 ? Math.abs(1 / dir.z) : Infinity;
    let tmx = dir.x !== 0 ? (stepX > 0 ? x + 1 - origin.x : origin.x - x) * tdx : Infinity;
    let tmy = dir.y !== 0 ? (stepY > 0 ? y + 1 - origin.y : origin.y - y) * tdy : Infinity;
    let tmz = dir.z !== 0 ? (stepZ > 0 ? z + 1 - origin.z : origin.z - z) * tdz : Infinity;
    let nx = 0, ny = 0, nz = 0, t = 0;

    for (let i = 0; i < 512; i++) {
      if (t > maxDist) return null;
      const id = this.getBlock(x, y, z);
      if (id !== 0 && BLOCKS.byId[id].solid) {
        return { block: id, x: x, y: y, z: z, nx: nx, ny: ny, nz: nz,
                 place: { x: x + nx, y: y + ny, z: z + nz }, distance: t };
      }
      if (tmx < tmy && tmx < tmz)      { x += stepX; t = tmx; tmx += tdx; nx = -stepX; ny = 0; nz = 0; }
      else if (tmy < tmz)              { y += stepY; t = tmy; tmy += tdy; ny = -stepY; nx = 0; nz = 0; }
      else                             { z += stepZ; t = tmz; tmz += tdz; nz = -stepZ; nx = 0; ny = 0; }
    }
    return null;
  }
}

/* ------- statics ------- */

World.SEED         = 1337;
World.CHUNK_SIZE   = 16;
World.HEIGHT       = 64;
World.CHUNKS_WIDE  = 8;                                 // 8x8 chunks -> 128x128 blocks
World.SIZE         = World.CHUNKS_WIDE * World.CHUNK_SIZE;
World.MIN          = -World.SIZE / 2;                   // world spans [MIN, MAX) on x/z
World.MAX          =  World.SIZE / 2;
World.SAND_LEVEL   = 9;
World.TREE_DENSITY = 0.018;
World.material     = new THREE.MeshLambertMaterial({ vertexColors: true, side: THREE.DoubleSide });

/* ------- exports (contract) ------- */

window.BLOCKS = BLOCKS;
window.World  = World;

})();
