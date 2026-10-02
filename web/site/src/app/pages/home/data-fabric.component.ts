import { isPlatformBrowser } from '@angular/common';
import {
  AfterViewInit,
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  NgZone,
  OnDestroy,
  PLATFORM_ID,
  ViewChild,
  inject,
} from '@angular/core';
import type * as THREE from 'three';
import type { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js';
import type { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js';

import { AtmosphereService } from './atmosphere.service';

const REDUCED_QUERY = '(prefers-reduced-motion: reduce)';
const BLOOM_QUERY = '(min-width: 901px)';
const PARTICLES = 1100;
const RESIZE_DEBOUNCE_MS = 150;
/**
 * Resolución interna del lienzo en píxeles CSS. El campo es suave por naturaleza, así que
 * renderizar a ~2/3 y dejar que el navegador lo reescale no se nota y deja el frame por debajo de 16 ms.
 */
const RENDER_SCALE_DESKTOP = 0.66;
const RENDER_SCALE_MOBILE = 0.55;
/* Un pelo por debajo de 16.67 ms para no saltarse frames por jitter del rAF. */
const MIN_FRAME_MS = 1000 / 60 - 2.5;
/**
 * Umbral del bloom según la atmósfera. Sobre fondo claro (luminancia ≈ 0.96) el umbral debe quedar
 * por encima del fondo, si no la imagen entera "florece" y se quema a blanco; solo las partículas
 * aditivas (que superan 1.0 en el render target HalfFloat) deben brillar. En oscuro baja para que
 * las sinapsis y partículas resplandezcan.
 */
const BLOOM_THRESHOLD_LIGHT = 1.02;
const BLOOM_THRESHOLD_DARK = 0.55;

const NOISE_GLSL = /* glsl */ `
vec3 mod289(vec3 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec4 mod289(vec4 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec4 permute(vec4 x) { return mod289(((x * 34.0) + 1.0) * x); }
vec4 taylorInvSqrt(vec4 r) { return 1.79284291400159 - 0.85373472095314 * r; }

float snoise(vec3 v) {
  const vec2 C = vec2(1.0 / 6.0, 1.0 / 3.0);
  const vec4 D = vec4(0.0, 0.5, 1.0, 2.0);
  vec3 i = floor(v + dot(v, C.yyy));
  vec3 x0 = v - i + dot(i, C.xxx);
  vec3 g = step(x0.yzx, x0.xyz);
  vec3 l = 1.0 - g;
  vec3 i1 = min(g.xyz, l.zxy);
  vec3 i2 = max(g.xyz, l.zxy);
  vec3 x1 = x0 - i1 + C.xxx;
  vec3 x2 = x0 - i2 + C.yyy;
  vec3 x3 = x0 - D.yyy;
  i = mod289(i);
  vec4 p = permute(permute(permute(
    i.z + vec4(0.0, i1.z, i2.z, 1.0))
    + i.y + vec4(0.0, i1.y, i2.y, 1.0))
    + i.x + vec4(0.0, i1.x, i2.x, 1.0));
  float n_ = 0.142857142857;
  vec3 ns = n_ * D.wyz - D.xzx;
  vec4 j = p - 49.0 * floor(p * ns.z * ns.z);
  vec4 x_ = floor(j * ns.z);
  vec4 y_ = floor(j - 7.0 * x_);
  vec4 x = x_ * ns.x + ns.yyyy;
  vec4 y = y_ * ns.x + ns.yyyy;
  vec4 h = 1.0 - abs(x) - abs(y);
  vec4 b0 = vec4(x.xy, y.xy);
  vec4 b1 = vec4(x.zw, y.zw);
  vec4 s0 = floor(b0) * 2.0 + 1.0;
  vec4 s1 = floor(b1) * 2.0 + 1.0;
  vec4 sh = -step(h, vec4(0.0));
  vec4 a0 = b0.xzyw + s0.xzyw * sh.xxyy;
  vec4 a1 = b1.xzyw + s1.xzyw * sh.zzww;
  vec3 p0 = vec3(a0.xy, h.x);
  vec3 p1 = vec3(a0.zw, h.y);
  vec3 p2 = vec3(a1.xy, h.z);
  vec3 p3 = vec3(a1.zw, h.w);
  vec4 norm = taylorInvSqrt(vec4(dot(p0, p0), dot(p1, p1), dot(p2, p2), dot(p3, p3)));
  p0 *= norm.x; p1 *= norm.y; p2 *= norm.z; p3 *= norm.w;
  vec4 m = max(0.6 - vec4(dot(x0, x0), dot(x1, x1), dot(x2, x2), dot(x3, x3)), 0.0);
  m = m * m;
  return 42.0 * dot(m * m, vec4(dot(p0, x0), dot(p1, x1), dot(p2, x2), dot(p3, x3)));
}

float fbm(vec3 p) {
  float value = 0.0;
  float amplitude = 0.5;
  for (int i = 0; i < 4; i++) {
    value += amplitude * snoise(p);
    p = p * 2.02 + vec3(1.7, 9.2, 0.0);
    amplitude *= 0.5;
  }
  return value;
}

float fbmLow(vec3 p) {
  float value = 0.0;
  float amplitude = 0.5;
  for (int i = 0; i < 3; i++) {
    value += amplitude * snoise(p);
    p = p * 2.02 + vec3(1.7, 9.2, 0.0);
    amplitude *= 0.5;
  }
  return value;
}
`;

/* El plano se dibuja en coordenadas de pantalla: cubre siempre el viewport sin importar la cámara. */
const FABRIC_VERTEX = /* glsl */ `
void main() {
  gl_Position = vec4(position.xy, 1.0, 1.0);
}
`;

const FABRIC_FRAGMENT = /* glsl */ `
precision highp float;
uniform float u_time;
uniform vec2 u_resolution;
uniform vec2 u_pointer;
uniform vec3 u_color1;
uniform vec3 u_color2;
uniform vec3 u_background;
uniform float u_dark;
${NOISE_GLSL}

float hash(vec2 p) {
  return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453123);
}

void main() {
  vec2 uv = gl_FragCoord.xy / u_resolution;
  vec2 p = (uv - 0.5) * vec2(u_resolution.x / u_resolution.y, 1.0);
  float t = u_time * 0.055;

  /* Deformación de dominio en dos pasos: el "líquido" fluye en vez de ondular.
     Las deformaciones usan menos octavas: solo el campo final necesita detalle. */
  vec2 q = vec2(fbmLow(vec3(p * 1.35, t)), fbmLow(vec3(p * 1.35 + 5.2, t + 1.3)));
  vec2 r = vec2(
    fbmLow(vec3(p * 1.15 + q * 1.7 + vec2(1.7, 9.2), t * 0.85)),
    fbmLow(vec3(p * 1.15 + q * 1.7 + vec2(8.3, 2.8), t * 0.95))
  );
  float field = fbm(vec3(p * 1.05 + r * 1.45 + u_pointer * 0.12, t));
  field = field * 0.5 + 0.5;

  /* Transiciones amplias: manchas de color que fluyen, no vetas de mármol. */
  vec3 color = mix(u_background, u_color1, smoothstep(0.18, 1.0, field) * mix(0.8, 0.7, u_dark));
  color = mix(color, u_color2, smoothstep(0.45, 1.1, length(q)) * mix(0.75, 0.6, u_dark));

  /* Vetas: en el modo oscuro son las "sinapsis" de la red; en claro casi no existen.
     Isolíneas anchas y suaves sobre un campo de baja frecuencia: las finas sobre 4 octavas
     se convertían en "estática" al renderizar a 0.66x. */
  float veinField = fbmLow(vec3(p * 0.9 + r * 1.1, t * 0.7)) * 0.5 + 0.5;
  /* Una sola banda suave por lóbulo del ruido (sin fract: nada de contornos topográficos). */
  float veins = smoothstep(0.40, 0.5, veinField) * (1.0 - smoothstep(0.5, 0.60, veinField));
  color += veins * mix(u_color1, u_color2, r.x * 0.5 + 0.5) * (0.01 + 0.22 * u_dark);

  color = mix(color, u_background, smoothstep(0.55, 1.25, length(p)) * 0.55);
  /* Grano: aquí y no en una capa DOM con feTurbulence + blend (esa capa costaba ~5 ms por frame).
     Cambia a 12 fps como grano de película, no a 60 fps como estática. */
  color += (hash(gl_FragCoord.xy + floor(u_time * 12.0)) - 0.5) * (0.008 + 0.004 * u_dark);

  gl_FragColor = vec4(color, 1.0);
}
`;

const PARTICLE_VERTEX = /* glsl */ `
attribute float aSize;
attribute float aSeed;
uniform float u_time;
uniform float u_pixelRatio;
uniform float u_fogDensity;
varying float vMix;
varying float vVisibility;

void main() {
  vec3 pos = position;
  pos.x += sin(u_time * 0.28 + aSeed * 6.2831) * 0.45;
  pos.y += cos(u_time * 0.22 + aSeed * 4.1) * 0.35;
  pos.z += sin(u_time * 0.17 + aSeed * 2.7) * 0.3;
  vec4 mvPosition = modelViewMatrix * vec4(pos, 1.0);
  float depth = max(-mvPosition.z, 0.001);
  gl_PointSize = aSize * u_pixelRatio * (14.0 / depth);
  gl_Position = projectionMatrix * mvPosition;

  /* FogExp2 aplicada a mano: con mezcla aditiva se desvanece el alfa, no se tiñe el color. */
  vVisibility = exp(-u_fogDensity * u_fogDensity * depth * depth) * smoothstep(0.0, 1.5, depth);
  vMix = aSeed;
}
`;

const PARTICLE_FRAGMENT = /* glsl */ `
precision highp float;
uniform vec3 u_particle1;
uniform vec3 u_particle2;
uniform float u_intensity;
varying float vMix;
varying float vVisibility;

void main() {
  float d = length(gl_PointCoord - 0.5);
  float core = smoothstep(0.5, 0.05, d);
  float halo = smoothstep(0.5, 0.3, d) * 0.35;
  vec3 color = mix(u_particle1, u_particle2, step(0.5, vMix)) * u_intensity;
  gl_FragColor = vec4(color, (core + halo) * vVisibility);
}
`;

interface Scene3D {
  three: typeof THREE;
  renderer: THREE.WebGLRenderer;
  scene: THREE.Scene;
  camera: THREE.PerspectiveCamera;
  fog: THREE.FogExp2;
  fabric: THREE.Mesh<THREE.PlaneGeometry, THREE.ShaderMaterial>;
  particles: THREE.Points<THREE.BufferGeometry, THREE.ShaderMaterial>;
  composer: EffectComposer;
  bloom: UnrealBloomPass | null;
  disposables: Array<{ dispose(): void }>;
}

/**
 * Fondo WebGL de toda la landing. Lee `AtmosphereService.state` en cada frame: el scroll (GSAP)
 * muta colores, niebla, bloom y cámara, y aquí solo se pintan.
 */
@Component({
  selector: 'app-data-fabric',
  standalone: true,
  template: '<canvas #canvas class="data-fabric-canvas" aria-hidden="true"></canvas>',
  styles: [
    `
      :host {
        position: fixed;
        inset: 0;
        z-index: 0;
        display: block;
        pointer-events: none;
      }
      .data-fabric-canvas {
        display: block;
        width: 100%;
        height: 100%;
      }
    `,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class DataFabricComponent implements AfterViewInit, OnDestroy {
  @ViewChild('canvas', { static: true }) private canvasRef!: ElementRef<HTMLCanvasElement>;

  private readonly zone = inject(NgZone);
  private readonly atmosphere = inject(AtmosphereService);
  private readonly isBrowser = isPlatformBrowser(inject(PLATFORM_ID));

  private scene3d: Scene3D | null = null;
  private raf = 0;
  private destroyed = false;
  private reduced = false;
  private lastVersion = -1;
  private resizeTimer = 0;
  private startTime = 0;
  private elapsed = 0;
  private lastRender = 0;
  private readonly pointerTarget = { x: 0, y: 0 };
  private cleanups: Array<() => void> = [];

  ngAfterViewInit(): void {
    if (!this.isBrowser) {
      return;
    }
    const canvas = this.canvasRef.nativeElement;
    const gl = canvas.getContext('webgl2') ?? canvas.getContext('webgl');
    if (!gl) {
      return;
    }
    this.zone.runOutsideAngular(() => {
      void this.boot(canvas);
    });
  }

  ngOnDestroy(): void {
    this.destroyed = true;
    if (this.raf) {
      cancelAnimationFrame(this.raf);
      this.raf = 0;
    }
    window.clearTimeout(this.resizeTimer);
    this.cleanups.forEach((fn) => fn());
    this.cleanups = [];
    const s = this.scene3d;
    if (s) {
      s.disposables.forEach((d) => d.dispose());
      s.scene.clear();
      s.renderer.dispose();
      s.renderer.forceContextLoss();
      this.scene3d = null;
    }
  }

  private async boot(canvas: HTMLCanvasElement): Promise<void> {
    const [three, { EffectComposer }, { RenderPass }, { UnrealBloomPass }, { OutputPass }] = await Promise.all([
      import('three'),
      import('three/examples/jsm/postprocessing/EffectComposer.js'),
      import('three/examples/jsm/postprocessing/RenderPass.js'),
      import('three/examples/jsm/postprocessing/UnrealBloomPass.js'),
      import('three/examples/jsm/postprocessing/OutputPass.js'),
    ]);
    if (this.destroyed) {
      return;
    }

    const reducedQuery = window.matchMedia(REDUCED_QUERY);
    const bloomQuery = window.matchMedia(BLOOM_QUERY);
    this.reduced = reducedQuery.matches;

    const renderer = new three.WebGLRenderer({
      canvas,
      antialias: false,
      alpha: false,
      powerPreference: 'high-performance',
      stencil: false,
      depth: false,
    });
    renderer.setPixelRatio(bloomQuery.matches ? RENDER_SCALE_DESKTOP : RENDER_SCALE_MOBILE);
    renderer.toneMapping = three.NoToneMapping;

    const state = this.atmosphere.state;
    const scene = new three.Scene();
    const fog = new three.FogExp2(new three.Color().setRGB(state.fogColor.r, state.fogColor.g, state.fogColor.b), state.fogDensity);
    scene.fog = fog;
    const camera = new three.PerspectiveCamera(55, 1, 0.1, 80);
    camera.position.set(0, 0, state.cameraZ);

    const fabricMaterial = new three.ShaderMaterial({
      vertexShader: FABRIC_VERTEX,
      fragmentShader: FABRIC_FRAGMENT,
      depthTest: false,
      depthWrite: false,
      uniforms: {
        u_time: { value: 0 },
        u_resolution: { value: new three.Vector2(1, 1) },
        u_pointer: { value: new three.Vector2() },
        u_color1: { value: new three.Color() },
        u_color2: { value: new three.Color() },
        u_background: { value: new three.Color() },
        u_dark: { value: 0 },
      },
    });
    const fabric = new three.Mesh(new three.PlaneGeometry(2, 2), fabricMaterial);
    fabric.frustumCulled = false;
    fabric.renderOrder = -1;
    scene.add(fabric);

    const positions = new Float32Array(PARTICLES * 3);
    const sizes = new Float32Array(PARTICLES);
    const seeds = new Float32Array(PARTICLES);
    for (let i = 0; i < PARTICLES; i++) {
      positions[i * 3] = (Math.random() - 0.5) * 30;
      positions[i * 3 + 1] = (Math.random() - 0.5) * 18;
      positions[i * 3 + 2] = -34 + Math.random() * 40;
      sizes[i] = 0.6 + Math.pow(Math.random(), 3) * 3.2;
      seeds[i] = Math.random();
    }
    const particleGeometry = new three.BufferGeometry();
    particleGeometry.setAttribute('position', new three.BufferAttribute(positions, 3));
    particleGeometry.setAttribute('aSize', new three.BufferAttribute(sizes, 1));
    particleGeometry.setAttribute('aSeed', new three.BufferAttribute(seeds, 1));
    const particleMaterial = new three.ShaderMaterial({
      vertexShader: PARTICLE_VERTEX,
      fragmentShader: PARTICLE_FRAGMENT,
      transparent: true,
      depthTest: false,
      depthWrite: false,
      blending: three.AdditiveBlending,
      uniforms: {
        u_time: { value: 0 },
        u_pixelRatio: { value: renderer.getPixelRatio() },
        u_fogDensity: { value: state.fogDensity },
        u_particle1: { value: new three.Color('#ff6b00') },
        u_particle2: { value: new three.Color('#25d366') },
        u_intensity: { value: 1 },
      },
    });
    const particles = new three.Points(particleGeometry, particleMaterial);
    particles.frustumCulled = false;
    scene.add(particles);

    const composer = new EffectComposer(renderer);
    composer.addPass(new RenderPass(scene, camera));
    let bloom: UnrealBloomPass | null = null;
    if (bloomQuery.matches) {
      bloom = new UnrealBloomPass(new three.Vector2(1, 1), state.bloomStrength, state.bloomRadius, BLOOM_THRESHOLD_LIGHT);
      composer.addPass(bloom);
    }
    composer.addPass(new OutputPass());

    this.scene3d = {
      three,
      renderer,
      scene,
      camera,
      fog,
      fabric,
      particles,
      composer,
      bloom,
      disposables: [fabric.geometry, fabricMaterial, particleGeometry, particleMaterial, composer],
    };

    this.bindEvents(reducedQuery);
    this.startTime = performance.now();
    /* `resize()` ya agenda el primer frame vía `requestFrame()`; agendar otro aquí dejaba dos
       bucles rAF vivos y el campo (con bloom) se renderizaba dos veces por frame. */
    this.resize();
  }

  private bindEvents(reducedQuery: MediaQueryList): void {
    const onResize = () => {
      window.clearTimeout(this.resizeTimer);
      this.resizeTimer = window.setTimeout(() => this.resize(), RESIZE_DEBOUNCE_MS);
    };
    window.addEventListener('resize', onResize, { passive: true });
    this.cleanups.push(() => window.removeEventListener('resize', onResize));

    /* El puntero solo guarda el objetivo; el frame lo consume (equivale a un throttle por rAF). */
    const onPointer = (event: PointerEvent) => {
      if (event.pointerType !== 'mouse') {
        return;
      }
      this.pointerTarget.x = (event.clientX / window.innerWidth) * 2 - 1;
      this.pointerTarget.y = -((event.clientY / window.innerHeight) * 2 - 1);
    };
    window.addEventListener('pointermove', onPointer, { passive: true });
    this.cleanups.push(() => window.removeEventListener('pointermove', onPointer));

    const onReduced = () => {
      this.reduced = reducedQuery.matches;
      this.lastVersion = -1;
      this.requestFrame();
    };
    reducedQuery.addEventListener('change', onReduced);
    this.cleanups.push(() => reducedQuery.removeEventListener('change', onReduced));

    const onVisibility = () => {
      if (!document.hidden) {
        this.startTime = performance.now() - this.elapsed * 1000;
        this.requestFrame();
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    this.cleanups.push(() => document.removeEventListener('visibilitychange', onVisibility));
  }

  private requestFrame(): void {
    if (!this.raf && !this.destroyed) {
      this.raf = requestAnimationFrame(this.frame);
    }
  }

  private resize(): void {
    const s = this.scene3d;
    if (!s) {
      return;
    }
    const width = window.innerWidth;
    const height = window.innerHeight;
    s.renderer.setSize(width, height, false);
    s.composer.setSize(width, height);
    s.bloom?.setSize(width / 2, height / 2);
    s.camera.aspect = width / height;
    s.camera.updateProjectionMatrix();
    const ratio = s.renderer.getPixelRatio();
    s.fabric.material.uniforms['u_resolution'].value.set(width * ratio, height * ratio);
    s.particles.material.uniforms['u_pixelRatio'].value = ratio;
    this.lastVersion = -1;
    this.requestFrame();
  }

  private readonly frame = (now: number): void => {
    this.raf = 0;
    const s = this.scene3d;
    if (!s || this.destroyed) {
      return;
    }
    if (document.hidden) {
      return;
    }
    /* Tope a ~60 renders/s: en pantallas de 120 Hz el campo no necesita el doble de pases de bloom. */
    if (now - this.lastRender < MIN_FRAME_MS) {
      this.raf = requestAnimationFrame(this.frame);
      return;
    }
    this.lastRender = now;

    const state = this.atmosphere.state;
    const version = this.atmosphere.version;
    const changed = version !== this.lastVersion;
    this.lastVersion = version;

    /* Con "reducir movimiento" el campo queda quieto: solo se repinta cuando el scroll cambia la atmósfera. */
    if (this.reduced && !changed) {
      this.raf = requestAnimationFrame(this.frame);
      return;
    }
    if (!this.reduced) {
      this.elapsed = (now - this.startTime) / 1000;
    }

    const pointer = this.atmosphere.pointer;
    pointer.x += (this.pointerTarget.x - pointer.x) * 0.045;
    pointer.y += (this.pointerTarget.y - pointer.y) * 0.045;

    const fabric = s.fabric.material.uniforms;
    fabric['u_time'].value = this.elapsed;
    fabric['u_pointer'].value.set(pointer.x, pointer.y);
    fabric['u_color1'].value.setRGB(state.color1.r, state.color1.g, state.color1.b);
    fabric['u_color2'].value.setRGB(state.color2.r, state.color2.g, state.color2.b);
    fabric['u_background'].value.setRGB(state.background.r, state.background.g, state.background.b);
    fabric['u_dark'].value = state.dark;

    const particle = s.particles.material.uniforms;
    particle['u_time'].value = this.elapsed;
    particle['u_fogDensity'].value = state.fogDensity;
    particle['u_intensity'].value = 0.55 + state.dark * 1.35;

    s.fog.color.setRGB(state.fogColor.r, state.fogColor.g, state.fogColor.b);
    s.fog.density = state.fogDensity;
    s.renderer.setClearColor(fabric['u_background'].value as THREE.Color, 1);

    if (s.bloom) {
      s.bloom.strength = state.bloomStrength;
      s.bloom.radius = state.bloomRadius;
      s.bloom.threshold = BLOOM_THRESHOLD_LIGHT + (BLOOM_THRESHOLD_DARK - BLOOM_THRESHOLD_LIGHT) * state.dark;
    }

    s.camera.position.x += (pointer.x * 0.45 - s.camera.position.x) * 0.05;
    s.camera.position.y += (pointer.y * 0.3 - s.camera.position.y) * 0.05;
    s.camera.position.z = state.cameraZ;
    s.camera.lookAt(0, 0, -6);

    s.composer.render();
    this.raf = requestAnimationFrame(this.frame);
  };
}
