// The rig in 3D: a Bambu Lab A1 with the PETG well grid, the Raspberry Pi 5 + camera + LED bar on the toolhead,
// on a white cabinet. Units are millimetres; world axes: x right, y up, z toward the viewer (the printer's front).
// Two views of the same scene:
//   - the "printer camera": a free orbit view, as if standing in front of the rig;
//   - the "nozzle camera": the Pi camera, rendered as a cube map from the lens and remapped through an
//     equidistant fisheye (f = 600 px for a 1280 x 720 frame, nadir at pixel (690.5, 362)), with the room light
//     off and the LED bar on, as during an experiment.
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { RoundedBoxGeometry } from "three/addons/geometries/RoundedBoxGeometry.js";
import { RectAreaLightUniformsLib } from "three/addons/lights/RectAreaLightUniformsLib.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
import { mergeGeometries } from "three/addons/utils/BufferGeometryUtils.js";

const BASE_H = 62;                 // printer base height above the cabinet top
const PLATE_TOP = BASE_H + 14;     // build-plate surface
const Z_NOZZLE = -20;              // world z of the nozzle (the toolhead rides the X rail in this plane)
const RAIL_Z = Z_NOZZLE - 60;
const COL_X = 207, COL_Z = -74, COL_H = 448;
const LAYER_PI_HIDDEN = 1;         // parts the Pi camera can't see (itself, the LED bar)
const LAYER_OVERLAY = 2;           // helpers drawn only in the printer view

function std(color, o = {}) { return new THREE.MeshStandardMaterial({ color, roughness: 0.6, metalness: 0, ...o }); }
function box(w, h, d, mat, r = 0) {
  const g = r > 0 ? new RoundedBoxGeometry(w, h, d, 3, r) : new THREE.BoxGeometry(w, h, d);
  const m = new THREE.Mesh(g, mat);
  m.castShadow = true; m.receiveShadow = true;
  return m;
}
function onLayer(obj, layer) { obj.traverse((o) => o.layers.set(layer)); return obj; }

function canvasTex(w, h, draw) {
  const c = document.createElement("canvas");
  c.width = w; c.height = h;
  draw(c.getContext("2d"), w, h);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 8;
  return t;
}
function label(text, { w = 512, h = 128, bg = null, fg = "#3a3d40", font = "600 64px system-ui, sans-serif", align = "center" } = {}) {
  return canvasTex(w, h, (g) => {
    if (bg) { g.fillStyle = bg; g.fillRect(0, 0, w, h); }
    g.fillStyle = fg; g.font = font; g.textBaseline = "middle"; g.textAlign = align;
    const lines = text.split("\n");
    lines.forEach((ln, i) => g.fillText(ln, align === "center" ? w / 2 : 16, h / 2 + (i - (lines.length - 1) / 2) * parseInt(font.match(/(\d+)px/)[1]) * 1.15));
  });
}
function decal(tex, w, h, transparent = true) {
  const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshStandardMaterial({ map: tex, transparent, roughness: 0.7 }));
  m.receiveShadow = true;
  return m;
}

function plateTexture() {
  // glossy black plate: faint diagonal hatch, and rainbow diffraction streaks that only show where the LED lights them
  return canvasTex(2048, 2048, (g, w, h) => {
    g.fillStyle = "#0a0a0c"; g.fillRect(0, 0, w, h);
    g.strokeStyle = "rgba(255,255,255,0.035)"; g.lineWidth = 1.2;
    for (let k = -w; k < w * 2; k += 96) {
      g.beginPath(); g.moveTo(k, 0); g.lineTo(k + h, h); g.stroke();
      g.beginPath(); g.moveTo(k, h); g.lineTo(k + h, 0); g.stroke();
    }
    let seed = 7; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
    for (let i = 0; i < 70; i++) {
      const x0 = rnd() * w, y0 = rnd() * h, len = 120 + rnd() * 420, dir = rnd() < 0.5 ? 1 : -1;
      const grad = g.createLinearGradient(x0, y0, x0 + len * 0.707, y0 + dir * len * 0.707);
      const hue = rnd() * 360;
      for (let s = 0; s <= 6; s++) grad.addColorStop(s / 6, `hsla(${(hue + s * 50) % 360}, 95%, 55%, ${0.5 * Math.sin(Math.PI * s / 6)})`);
      g.strokeStyle = grad; g.lineWidth = 2 + rnd() * 3;
      g.beginPath(); g.moveTo(x0, y0); g.lineTo(x0 + len * 0.707, y0 + dir * len * 0.707); g.stroke();
    }
  });
}

export class RigScene {
  constructor(viewEl, piEl, geo) {
    this.geo = geo;
    this.viewEl = viewEl; this.piEl = piEl;
    this.state = null; this.recvWall = 0;
    this.wellMeshes = {};
    this._cableKey = "";
    RectAreaLightUniformsLib.init();

    // renderers -------------------------------------------------------------------------------
    this.r1 = new THREE.WebGLRenderer({ antialias: true });
    this.r1.setPixelRatio(Math.min(2, window.devicePixelRatio || 1));
    this.r1.shadowMap.enabled = true;
    this.r1.shadowMap.type = THREE.PCFSoftShadowMap;
    this.r1.toneMapping = THREE.ACESFilmicToneMapping;
    this.r1.toneMappingExposure = 1.0;
    viewEl.prepend(this.r1.domElement);
    this.r2 = new THREE.WebGLRenderer({ antialias: false });
    this.r2.setPixelRatio(Math.min(1.5, window.devicePixelRatio || 1));
    this.r2.toneMapping = THREE.NoToneMapping;
    piEl.prepend(this.r2.domElement);

    // scene ------------------------------------------------------------------------------------
    const scene = this.scene = new THREE.Scene();
    scene.background = new THREE.Color(0xe9e4dc);
    const pmrem = new THREE.PMREMGenerator(this.r1);
    this.envTex = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
    scene.environment = this.envTex;
    scene.environmentIntensity = 0.45;

    this.hemi = new THREE.HemisphereLight(0xfff3e4, 0x6f685f, 0.9);
    scene.add(this.hemi);
    this.key = new THREE.DirectionalLight(0xfff0dc, 2.4);
    this.key.position.set(-650, 1150, 720);
    this.key.target.position.set(0, 100, 0);
    this.key.castShadow = true;
    this.key.shadow.mapSize.set(2048, 2048);
    Object.assign(this.key.shadow.camera, { left: -520, right: 520, top: 620, bottom: -420, near: 200, far: 3200 });
    this.key.shadow.bias = -0.0004;
    scene.add(this.key, this.key.target);
    this.fill = new THREE.DirectionalLight(0xdfe7ff, 0.55);
    this.fill.position.set(700, 500, -300);
    scene.add(this.fill);

    this._room();
    this._base();
    this._frame();
    this._bed();
    this._gantry();
    this._toolhead();

    // printer camera (free view) --------------------------------------------------------------
    this.cam = new THREE.PerspectiveCamera(36, 16 / 9, 5, 20000);
    this.cam.layers.enable(LAYER_PI_HIDDEN);
    this.cam.layers.enable(LAYER_OVERLAY);
    this.controls = new OrbitControls(this.cam, this.r1.domElement);
    this.controls.enableDamping = true;
    this.controls.minDistance = 160; this.controls.maxDistance = 2600;
    this.controls.maxPolarAngle = 1.52;
    this.resetView();

    // nozzle camera (fisheye from a cube map) --------------------------------------------------
    this.cubeRT = new THREE.WebGLCubeRenderTarget(640, { type: THREE.HalfFloatType, generateMipmaps: false });
    this.cubeCam = new THREE.CubeCamera(1, 6000, this.cubeRT);
    scene.add(this.cubeCam);
    const tilt = geo.tilt_rad;
    this.fisheye = new THREE.ShaderMaterial({
      uniforms: {
        tCube: { value: this.cubeRT.texture }, fpx: { value: geo.f_px }, frame: { value: new THREE.Vector2(...geo.frame_px) },
        camRight: { value: new THREE.Vector3(Math.cos(tilt), -Math.sin(tilt), 0) },
        camUp: { value: new THREE.Vector3(0, 0, -1) },
        camFwd: { value: new THREE.Vector3(-Math.sin(tilt), -Math.cos(tilt), 0) },
        time: { value: 0 }, exposure: { value: 2.6 }, nadir: { value: new THREE.Vector2(...geo.nadir_px) }, showNadir: { value: 0 },
      },
      vertexShader: "varying vec2 vUv; void main(){ vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }",
      fragmentShader: `
        uniform samplerCube tCube; uniform float fpx; uniform vec2 frame; uniform vec3 camRight, camUp, camFwd;
        uniform float time, exposure, showNadir; uniform vec2 nadir; varying vec2 vUv;
        float hash(vec2 p){ return fract(sin(dot(p, vec2(12.9898, 78.233)) + time * 0.37) * 43758.5453); }
        void main(){
          vec2 px = vec2(vUv.x * frame.x, (1.0 - vUv.y) * frame.y);      // image pixels, y down
          vec2 d = px - 0.5 * frame;
          float r = length(d), th = r / fpx;                               // equidistant fisheye
          vec2 u = r > 1e-4 ? d / r : vec2(0.0);
          vec3 dir = normalize(cos(th) * camFwd + sin(th) * (u.x * camRight - u.y * camUp));
          vec3 c = textureCube(tCube, dir).rgb;
          // slight chromatic fringing toward the edge of the lens
          float ca = 0.0016 * th * th;
          c.r = textureCube(tCube, normalize(dir + ca * vec3(u.x, 0.0, u.y))).r;
          c *= exposure;
          c = vec3(1.0) - exp(-c);                                         // highlights roll off like the sensor's
          c = c * vec3(0.95, 1.0, 0.98) + vec3(0.016, 0.002, 0.021);       // white balance; the bed's purple black level
          c *= 1.0 - 0.42 * pow(clamp(r / length(0.5 * frame), 0.0, 1.0), 2.4); // vignetting
          c += (hash(px) - 0.5) * 0.018;                                   // sensor noise
          if (showNadir > 0.5) {
            vec2 q = abs(px - nadir);
            if ((q.x < 1.2 && q.y < 14.0) || (q.y < 1.2 && q.x < 14.0)) c = mix(c, vec3(1.0, 0.25, 0.2), 0.85);
          }
          gl_FragColor = vec4(clamp(c, 0.0, 1.0), 1.0);
          #include <colorspace_fragment>
        }`,
      depthTest: false, depthWrite: false,
    });
    this.piScene = new THREE.Scene();
    this.piQuad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), this.fisheye);
    this.piQuad.frustumCulled = false;
    this.piScene.add(this.piQuad);
    this.piCam = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
    this.lastPi = 0;

    new ResizeObserver(() => this._resize()).observe(viewEl);
    new ResizeObserver(() => this._resize()).observe(piEl);
    this._resize();
    this.r1.setAnimationLoop((t) => this._frame_(t));
  }

  resetView() {
    this.cam.position.set(-360, 560, 860);
    this.controls.target.set(10, 170, -20);
    this.controls.update();
  }

  // ------------------------------------------------------------------------------------- building
  _room() {
    const s = this.scene;
    const wall = box(2600, 1800, 20, std(0xefe9df, { roughness: 0.95 }));
    wall.position.set(0, 300, -420); wall.castShadow = false; s.add(wall);
    const floor = box(2600, 20, 2000, std(0xb98d5f, { roughness: 0.8 }));
    floor.position.set(0, -620, 300); floor.castShadow = false; s.add(floor);
    const top = box(820, 22, 560, std(0xf3f3f1, { roughness: 0.5 }), 3);
    top.position.set(40, -11, 0); s.add(top);
    const cab = box(800, 590, 540, std(0xf6f6f4, { roughness: 0.6 }));
    cab.position.set(40, -318, -8); s.add(cab);
    for (const y of [-160, -400]) {          // drawer gaps and handles
      const gap = box(780, 2, 2, std(0xd8d8d4)); gap.position.set(40, y, 263); s.add(gap);
      const knob = new THREE.Mesh(new THREE.CylinderGeometry(5, 5, 26, 16), std(0xcfcfcb, { metalness: 0.6, roughness: 0.3 }));
      knob.rotation.x = Math.PI / 2; knob.position.set(250, y - 60, 274); s.add(knob);
    }
    const door = box(20, 1500, 780, std(0xc18d55, { roughness: 0.55 }));
    door.position.set(-560, 130, -40); s.add(door);
    for (const [y, h] of [[480, 420], [-40, 420]]) {
      const panel = box(6, h, 300, std(0xb47f49, { roughness: 0.55 }));
      panel.position.set(-548, y, -40); s.add(panel);
    }
    // the printer's touch screen on its stand, front right
    const scr = new THREE.Group();
    const bezel = box(118, 92, 12, std(0xf1f1ef, { roughness: 0.4 }), 6);
    const glass = new THREE.Mesh(new THREE.PlaneGeometry(96, 70), std(0x111214, { roughness: 0.15, metalness: 0.1 }));
    glass.position.z = 6.2;
    scr.add(bezel, glass);
    scr.position.set(232, 70, 205); scr.rotation.set(-0.35, -0.55, 0); s.add(scr);
  }

  _base() {
    const s = this.scene;
    const grey = std(0xe7e7e4, { roughness: 0.55 });
    const base = box(372, BASE_H, 410, grey, 14);
    base.position.set(0, BASE_H / 2, 8); s.add(base);
    const shelf = box(372, 30, 70, grey, 10);   // the lower front ledge with the logo
    shelf.position.set(0, 15, 230); s.add(shelf);
    const logo = decal(label("▦  Bambu Lab", { w: 1024, h: 160, fg: "#3b3f43", font: "700 92px system-ui, sans-serif" }), 160, 25);
    logo.position.set(-70, 15, 265.2); s.add(logo);
    const green = box(16, 16, 1, std(0x2fb36c, { roughness: 0.4 }));
    green.position.set(-151, 15, 265.4); s.add(green);
    const led = new THREE.Mesh(new THREE.BoxGeometry(5, 2, 1), new THREE.MeshBasicMaterial({ color: 0x3ab0ff }));
    led.position.set(150, 26, 265.3); s.add(led);
    for (const x of [-58, 58]) {                // slots the Y carriage runs in
      const slot = box(5, 1, 300, std(0x6d6f72, { roughness: 0.8 }));
      slot.position.set(x, BASE_H + 0.3, 40); s.add(slot);
    }
    const cover = box(100, 1, 120, std(0xdededb, { roughness: 0.5 }), 0.4);
    cover.position.set(0, BASE_H + 0.4, 130); s.add(cover);
    const sticker = decal(label("Safety Zone", { w: 512, h: 512, bg: "#f7f7f5", fg: "#2fa05f", font: "700 64px system-ui" }), 70, 70);
    sticker.rotation.x = -Math.PI / 2; sticker.position.set(-130, BASE_H + 0.5, 150); s.add(sticker);
  }

  _frame() {
    const s = this.scene;
    const alu = std(0xc9cbce, { metalness: 0.65, roughness: 0.33 });
    const dark = std(0x55585c, { roughness: 0.55 });
    for (const sx of [-1, 1]) {
      const col = box(36, COL_H - 60, 50, alu, 4);
      col.position.set(sx * COL_X, BASE_H / 2 + (COL_H - 60) / 2 + 10, COL_Z); s.add(col);
      const slot = box(4, COL_H - 110, 16, std(0x1c1d1f, { roughness: 0.7 }));
      slot.position.set(sx * (COL_X - 18.5), COL_H / 2 + 20, COL_Z); s.add(slot);
      const foot = box(44, 56, 60, dark, 6);
      foot.position.set(sx * COL_X, 28, COL_Z); s.add(foot);
      const corner = box(44, 46, 56, dark, 6);
      corner.position.set(sx * COL_X, COL_H + 8, COL_Z); s.add(corner);
    }
    const top = box(2 * COL_X - 40, 38, 48, alu, 4);
    top.position.set(0, COL_H + 8, COL_Z); s.add(top);
    const st1 = decal(label("Bambu Lab   |   MakerWorld", { w: 1024, h: 128, bg: "#ffffff", fg: "#222", font: "600 60px system-ui" }), 92, 11.5, false);
    st1.position.set(-120, COL_H + 8, COL_Z + 24.3); s.add(st1);
    const st2 = decal(label("Bambu Lab\nLET'S MAKE IT", { w: 512, h: 256, bg: "#1b1c1e", fg: "#f2f2f2", font: "700 58px system-ui" }), 30, 15, false);
    st2.position.set(-COL_X, 300, COL_Z + 25.3); s.add(st2);
    const st3 = decal(label("MakerWorld", { w: 512, h: 160, bg: "#1b1c1e", fg: "#f2f2f2", font: "700 64px system-ui" }), 30, 10, false);
    st3.position.set(COL_X, 300, COL_Z + 25.3); s.add(st3);
    // a brass speaker puck sits on top of the frame in the photos
    const puck = new THREE.Mesh(new THREE.CylinderGeometry(42, 42, 22, 48), std(0xc5a35a, { metalness: 0.8, roughness: 0.35 }));
    puck.position.set(10, COL_H + 39, COL_Z); puck.castShadow = true; s.add(puck);
    this.cableAnchor = new THREE.Vector3(COL_X - 10, COL_H - 30, COL_Z + 30);
  }

  _bed() {
    const g = this.bed = new THREE.Group();
    this.scene.add(g);
    g.position.y = PLATE_TOP;
    const carriage = box(150, 8, 220, std(0x2e3033, { roughness: 0.7 }));
    carriage.position.y = -12; g.add(carriage);
    const carrier = box(270, 8, 268, std(0xdcdcd9, { roughness: 0.5 }), 3);
    carrier.position.y = -5; g.add(carrier);
    const plate = new THREE.Mesh(new THREE.BoxGeometry(257, 1, 257), new THREE.MeshPhysicalMaterial({
      color: 0xffffff, map: plateTexture(), roughness: 0.2, metalness: 0.0, clearcoat: 1.0, clearcoatRoughness: 0.05 }));
    plate.position.y = -0.5; plate.receiveShadow = true; g.add(plate);
    const strip = decal(label("⚠ WARNING HOT SURFACE     BUILD VOLUME | 256 × 256 × 256 mm", { w: 2048, h: 96, bg: "#f4f4f2", fg: "#444", font: "600 46px system-ui" }), 240, 7, false);
    strip.position.set(0, -5, 134.2); g.add(strip);
    // the five PETG islands, from the twin's geometry (bed coordinates of each island's centre and rotation)
    const petg = std(0xf4f4f1, { roughness: 0.62 });
    const { wall, wall_h, pitch } = this.geo;
    for (const [name, isl] of Object.entries(this.geo.islands)) {
      const n = isl.n, size = isl.size, parts = [];
      for (let i = 0; i <= n; i++) {
        const c = -size / 2 + wall / 2 + i * pitch;
        const a = new THREE.BoxGeometry(wall, wall_h, size); a.translate(c, wall_h / 2, 0); parts.push(a);
        const b = new THREE.BoxGeometry(size, wall_h, wall); b.translate(0, wall_h / 2, c); parts.push(b);
      }
      const mesh = new THREE.Mesh(mergeGeometries(parts), petg);
      mesh.castShadow = true; mesh.receiveShadow = true;
      mesh.position.set(isl.center[0] - 128, 0, -(isl.center[1] - 128));
      mesh.rotation.y = THREE.MathUtils.degToRad(isl.rotation_deg);
      mesh.name = name;
      g.add(mesh);
    }
    // liquid in each well (hidden until loaded)
    const liqGeo = new THREE.BoxGeometry(9.7, 3, 9.7); liqGeo.translate(0, 1.5, 0);
    for (const [lab, w] of Object.entries(this.geo.wells)) {
      const m = new THREE.Mesh(liqGeo, new THREE.MeshPhysicalMaterial({ color: 0xd8dcd6, roughness: 0.08, transparent: true, opacity: 0.3, clearcoat: 1, clearcoatRoughness: 0.1 }));
      const [bx, by] = w.bed;
      m.position.set(bx - 128, 0.02, -(by - 128));
      const isl = this.geo.islands[lab.split("-")[0]];
      m.rotation.y = THREE.MathUtils.degToRad(isl.rotation_deg);
      m.visible = false; m.receiveShadow = true;
      g.add(m);
      this.wellMeshes[lab] = m;
    }
    // operator request marker
    this.ring = onLayer(new THREE.Mesh(new THREE.TorusGeometry(9, 0.9, 10, 48), new THREE.MeshBasicMaterial({ color: 0xfab219 })), LAYER_OVERLAY);
    this.ring.rotation.x = -Math.PI / 2; this.ring.visible = false;
    g.add(this.ring);
  }

  _gantry() {
    const g = this.gantry = new THREE.Group();
    this.scene.add(g);
    const alu = std(0xc6c8cb, { metalness: 0.6, roughness: 0.35 });
    const beam = box(2 * COL_X - 20, 36, 30, alu, 3);
    beam.position.set(0, 52, RAIL_Z); g.add(beam);
    const rail = box(2 * COL_X - 40, 15, 4, std(0xb8bcc0, { metalness: 0.85, roughness: 0.25 }));
    rail.position.set(0, 50, RAIL_Z + 17); g.add(rail);
    const boltGeo = new THREE.CylinderGeometry(2.6, 2.6, 1.2, 16); boltGeo.rotateX(Math.PI / 2);
    const boltMat = std(0x2b2d30, { metalness: 0.5, roughness: 0.4 });
    for (let x = -COL_X + 40; x <= COL_X - 40; x += 26) {
      const b = new THREE.Mesh(boltGeo, boltMat); b.position.set(x, 50, RAIL_Z + 19.4); g.add(b);
    }
    const left = box(42, 64, 54, std(0x4f5256, { roughness: 0.55 }), 5);
    left.position.set(-COL_X - 6, 50, RAIL_Z - 4); g.add(left);
    const hook = box(14, 52, 34, std(0x3d4043, { roughness: 0.6 }), 3);
    hook.position.set(-COL_X - 34, 18, RAIL_Z + 18); g.add(hook);
    const motor = box(60, 76, 74, std(0xeef0ee, { roughness: 0.45 }), 8);
    motor.position.set(COL_X + 18, 52, RAIL_Z + 8); g.add(motor);
    const mlab = decal(label("Bambu\nLab   A1", { w: 512, h: 384, fg: "#3b3f43", font: "700 92px system-ui" }), 48, 36);
    mlab.position.set(COL_X + 18, 56, RAIL_Z + 45.2); g.add(mlab);
  }

  _toolhead() {
    const t = this.tool = new THREE.Group();
    this.gantry.add(t);
    const white = std(0xeceeed, { roughness: 0.45 });
    const dark = std(0x3c3f42, { roughness: 0.6 });
    const body = box(54, 66, 46, white, 6);       // front face 4 mm ahead of the nozzle; the camera sits ~29 mm further out
    body.position.set(0, 24 + 33, -19); t.add(body);
    const back = box(46, 40, 14, std(0xd7d9d8, { roughness: 0.5 }), 4);   // carriage on the rail
    back.position.set(0, 52, RAIL_Z - Z_NOZZLE + 22); t.add(back);
    const fan = new THREE.Mesh(new THREE.CylinderGeometry(12, 12, 1.5, 32), std(0x1d1e20, { roughness: 0.6 }));
    fan.rotation.x = Math.PI / 2; fan.position.set(-13, 38, 4.6); t.add(fan);
    const hub = new THREE.Mesh(new THREE.CylinderGeometry(4, 4, 2, 16), std(0x2d2f31)); hub.rotation.x = Math.PI / 2; hub.position.set(-13, 38, 5.4); t.add(hub);
    const brand = decal(label("Bambu Lab", { w: 512, h: 128, fg: "#4a4d50", font: "700 70px system-ui" }), 26, 6.5);
    brand.position.set(9, 32, 4.3); t.add(brand);
    for (const sx of [-1, 1]) {           // the dark fan-duct lobes either side of the nozzle
      const lobe = box(26, 20, 34, dark, 7);
      lobe.position.set(sx * 19, 13, -5); t.add(lobe);
    }
    const shroud = box(14, 14, 12, std(0x18191b, { roughness: 0.5 }), 2);
    shroud.position.set(0, 14, -1); t.add(shroud);
    const caution = decal(label("≋\nCAUTION HOT", { w: 256, h: 256, bg: "#18191b", fg: "#e69a2e", font: "700 50px system-ui" }), 12, 12, false);
    caution.position.set(0, 14, 5.1); t.add(caution);
    const nozzle = new THREE.Mesh(new THREE.ConeGeometry(2.4, 6, 16), std(0xb48a3c, { metalness: 0.8, roughness: 0.3 }));
    nozzle.rotation.x = Math.PI; nozzle.position.set(0, 3, 0); t.add(nozzle);

    // printed bracket + Raspberry Pi 5 in the official red/white case, standing on the toolhead
    const bracket = box(50, 30, 44, std(0xe1e2e0, { roughness: 0.6 }), 3);
    bracket.position.set(-4, 104, -19); t.add(bracket);
    for (const sx of [-1, 1]) {
      const win = box(1, 20, 30, std(0x2a2b2d)); win.position.set(-4 + sx * 25.2, 104, -19); t.add(win);
    }
    const pi = new THREE.Group();
    pi.position.set(-4, 166, -21);
    t.add(pi);
    const red = box(64, 94, 30, std(0xc8133f, { roughness: 0.45 }), 7);
    pi.add(red);
    const bezel = box(64, 94, 4, std(0xf6f6f5, { roughness: 0.4 }), 4);
    bezel.position.z = 15.5; pi.add(bezel);
    const lid = box(48, 66, 1, new THREE.MeshStandardMaterial({ color: 0x9aa3a8, roughness: 0.1, transparent: true, opacity: 0.55 }));
    lid.position.set(0, 4, 17.8); pi.add(lid);
    const pfan = new THREE.Mesh(new THREE.CylinderGeometry(13, 13, 1, 32), std(0x202123)); pfan.rotation.x = Math.PI / 2; pfan.position.set(0, 14, 17.4); pi.add(pfan);
    const logo = decal(label("❀", { w: 128, h: 128, fg: "#b9bcbf", font: "400 96px system-ui" }), 9, 9);
    logo.position.set(0, -18, 18.5); pi.add(logo);
    const puck = new THREE.Mesh(new THREE.CylinderGeometry(17, 17, 12, 32), std(0xf1b6ae, { roughness: 0.5 }));
    puck.rotation.z = Math.PI / 2; puck.position.set(-38, 140, -17); puck.castShadow = true; t.add(puck);
    this.piTop = new THREE.Vector3(-4, 214, -27);

    // the camera: board in a clear acrylic mount in front of the toolhead, lens pointing down
    const L = this.geo.lens_above_nozzle, [ox, oy] = this.geo.camera_offset;
    const camGroup = new THREE.Group();
    camGroup.position.set(ox, L, -oy);
    t.add(camGroup);
    const acrylic = new THREE.MeshStandardMaterial({ color: 0xe8f4f8, roughness: 0.05, transparent: true, opacity: 0.35 });
    const arm = box(30, 3, 30, acrylic); arm.position.set(-2, 9, -15); camGroup.add(arm);
    const plateA = box(30, 18, 3, acrylic); plateA.position.set(-2, 1, -28); camGroup.add(plateA);
    const board = box(25, 2, 24, std(0x1f3b2c, { roughness: 0.6 })); board.position.set(0, 4, 0); camGroup.add(board);
    const lens = new THREE.Mesh(new THREE.CylinderGeometry(4.5, 4.5, 7, 24), std(0x0e0f10, { roughness: 0.3 }));
    lens.position.set(0, 0, 0); camGroup.add(lens);
    onLayer(camGroup, LAYER_PI_HIDDEN);
    this.lensLocal = new THREE.Vector3(ox, L - 3.5, -oy);
    this.ribbonStart = new THREE.Vector3(ox + 6, L + 5, -oy - 8);

    // the battery LED bar on its gooseneck, ~45 mm in front of the lens and a little above it: its reflection in
    // the plate then sits ~115 px below the nadir, as in calibration/verify_C-A1.jpg
    const ledGroup = new THREE.Group();
    ledGroup.position.set(13, L + 12, -oy + 45);
    t.add(ledGroup);
    const bar = box(120, 12, 22, std(0xf4b7a9, { roughness: 0.55 }), 5);
    ledGroup.add(bar);
    const emit = new THREE.Mesh(new THREE.PlaneGeometry(104, 10), new THREE.MeshBasicMaterial({ color: 0xfff6e8 }));
    emit.rotation.x = Math.PI / 2; emit.position.y = -6.2; ledGroup.add(emit);
    onLayer(ledGroup, LAYER_PI_HIDDEN);
    this.led = new THREE.RectAreaLight(0xfff1de, 55, 104, 10);
    this.led.position.set(13, L + 5.5, -oy + 45);
    this.led.rotation.x = -Math.PI / 2;  // emit straight down
    t.add(this.led);
    const neck = new THREE.Mesh(new THREE.TubeGeometry(new THREE.CatmullRomCurve3([
      new THREE.Vector3(-44, 140, -17), new THREE.Vector3(-70, 125, 10), new THREE.Vector3(-62, L + 22, -oy + 30), new THREE.Vector3(-45, L + 13, -oy + 45),
    ]), 40, 1.6, 8), std(0xbfc2c5, { metalness: 0.9, roughness: 0.25 }));
    neck.castShadow = true; t.add(neck);
    this._ribbon = null; this._cable = null;
  }

  _rebuildCables() {
    // ribbon from the camera up to the Pi; black power cable from the Pi to the top of the frame
    const t = this.tool;
    if (!this._ribbon) {
      const curve = new THREE.CatmullRomCurve3([this.ribbonStart, new THREE.Vector3(22, 120, 18), new THREE.Vector3(12, 150, 8), new THREE.Vector3(2, 130, 0)]);
      this._ribbon = new THREE.Mesh(new THREE.TubeGeometry(curve, 40, 1.4, 6), std(0xd68a2a, { roughness: 0.5 }));
      t.add(this._ribbon);
    }
    const start = t.localToWorld(this.piTop.clone());
    const end = this.cableAnchor;
    const key = `${start.x.toFixed(0)},${start.y.toFixed(0)},${start.z.toFixed(0)}`;
    if (key === this._cableKey) return;
    this._cableKey = key;
    const mid = start.clone().lerp(end, 0.5); mid.y = Math.max(start.y, end.y) + 70; mid.z += 60;
    const geo = new THREE.TubeGeometry(new THREE.CatmullRomCurve3([start, start.clone().add(new THREE.Vector3(30, 60, 10)), mid, end]), 48, 3.2, 8);
    if (!this._cable) {
      this._cable = new THREE.Mesh(geo, std(0x1b1c1e, { roughness: 0.55 }));
      this._cable.castShadow = true;
      this.scene.add(this._cable);
    } else { this._cable.geometry.dispose(); this._cable.geometry = geo; }
  }

  // ------------------------------------------------------------------------------------- state
  setState(s) { this.state = s; this.recvWall = performance.now(); }

  simNow() {
    const s = this.state;
    return s ? s.t + (performance.now() - this.recvWall) / 1000 * s.speed : 0;
  }

  nozzle() {
    const s = this.state;
    if (!s) return [128, 128, 62];
    const seg = s.seg, t = this.simNow();
    if (!seg || t >= seg.t0 + seg.T) return seg ? seg.p1 : s.pos;
    if (t <= seg.t0) return seg.p0;
    const tau = t - seg.t0, { a, ta, vp, T, d } = seg;
    let sd;
    if (tau < ta) sd = 0.5 * a * tau * tau;
    else if (tau > T - ta) sd = d - 0.5 * a * (T - tau) ** 2;
    else sd = 0.5 * a * ta * ta + vp * (tau - ta);
    const f = d > 0 ? sd / d : 1;
    return seg.p0.map((v, i) => v + (seg.p1[i] - v) * f);
  }

  _applyPose() {
    const [x, y, z] = this.nozzle();
    this.bed.position.z = Z_NOZZLE + (y - 128);
    this.gantry.position.y = PLATE_TOP + z;
    this.tool.position.set(x - 128, 0, Z_NOZZLE);
    return [x, y, z];
  }

  _applyWells() {
    const wells = (this.state && this.state.wells) || null;
    if (!wells) return;
    for (const [lab, m] of Object.entries(this.wellMeshes)) {
      const w = wells[lab];
      if (!w || w.fill <= 0.001) { m.visible = false; continue; }
      m.visible = true;
      m.scale.y = Math.max(0.02, w.fill);
      const W = w.w;
      const mat = m.material;
      const clear = new THREE.Color(0.62, 0.66, 0.64), cooked = new THREE.Color(0.93, 0.95, 0.91);
      mat.color.copy(clear).lerp(cooked, Math.min(1, W * 1.1));
      mat.opacity = 0.22 + 0.78 * Math.min(1, Math.pow(W / 0.6, 0.8));
      mat.roughness = 0.08 + 0.3 * W;
    }
  }

  wellWorld(label) {
    const w = this.geo.wells[label];
    if (!w) return null;
    const v = new THREE.Vector3(w.bed[0] - 128, 12, -(w.bed[1] - 128));
    return this.bed.localToWorld(v);
  }

  project(v) {
    const p = v.clone().project(this.cam);
    const r = this.r1.domElement.getBoundingClientRect();
    return { x: (p.x + 1) / 2 * r.width, y: (1 - p.y) / 2 * r.height, visible: p.z < 1 };
  }

  // ------------------------------------------------------------------------------------- render loop
  _resize() {
    for (const [r, el] of [[this.r1, this.viewEl], [this.r2, this.piEl]]) {
      const w = el.clientWidth, h = el.clientHeight;
      if (w && h) r.setSize(w, h, false);
    }
    const w = this.viewEl.clientWidth, h = this.viewEl.clientHeight;
    if (w && h) { this.cam.aspect = w / h; this.cam.updateProjectionMatrix(); }
  }

  _frame_(tms) {
    this._applyPose();
    this._applyWells();
    this._rebuildCables();
    const op = this.state && this.state.op;
    if (op && op.label && this.wellMeshes[op.label]) {
      const w = this.geo.wells[op.label];
      this.ring.visible = true;
      this.ring.position.set(w.bed[0] - 128, 11, -(w.bed[1] - 128));
      const k = 1 + 0.12 * Math.sin(tms / 180);
      this.ring.scale.set(k, k, k);
    } else this.ring.visible = false;
    this.controls.update();
    this.r1.render(this.scene, this.cam);
    if (tms - this.lastPi > 66) {  // ~15 fps for the nozzle camera
      this.lastPi = tms;
      this._renderPi(tms);
    }
    if (this.onFrame) this.onFrame();
  }

  _renderPi(tms) {
    const s = this.scene;
    const save = [this.hemi.intensity, this.key.intensity, this.fill.intensity, s.background, s.environment, this.ring.visible];
    // experiments run with the room light off: only a little daylight and the LED bar
    this.hemi.intensity = 0.05; this.key.intensity = 0.06; this.fill.intensity = 0.0;
    s.background = new THREE.Color(0x050505); s.environment = null; this.ring.visible = false;
    this.cubeCam.position.copy(this.tool.localToWorld(this.lensLocal.clone()));
    this.cubeCam.update(this.r2, s);
    [this.hemi.intensity, this.key.intensity, this.fill.intensity, s.background, s.environment, this.ring.visible] = save;
    this.fisheye.uniforms.time.value = tms / 1000;
    this.r2.render(this.piScene, this.piCam);
  }

  setNadirMarker(on) { this.fisheye.uniforms.showNadir.value = on ? 1 : 0; }
}
