import { isPlatformBrowser } from '@angular/common';
import {
  AfterViewInit,
  Directive,
  ElementRef,
  EventEmitter,
  NgZone,
  OnDestroy,
  Output,
  PLATFORM_ID,
  inject,
} from '@angular/core';

/** Debe coincidir con el `@media` del escenario en `home.component.scss`. */
const SCENE_QUERY = '(min-width: 901px) and (prefers-reduced-motion: no-preference)';
const POINTER_QUERY = '(hover: hover) and (pointer: fine)';
const REDUCED_QUERY = '(prefers-reduced-motion: reduce)';

/** Distancia de la cámara en px; el CSS usa el mismo `perspective` para el teléfono en reposo. */
const CAMERA = 1200;
/** Giro máximo de todo el escenario siguiendo al cursor, en grados. */
const SCENE_YAW = 7;
const SCENE_PITCH = 5;
/** Profundidad extra desde la que llegan las capas con `enter` al montar el escenario. */
const ENTER_DEPTH = -320;
const DEG = Math.PI / 180;
const REST = 0.0002;

interface SpringConfig {
  stiffness: number;
  damping: number;
}

const SCROLL_SPRING: SpringConfig = { stiffness: 100, damping: 20 };
const POINTER_SPRING: SpringConfig = { stiffness: 80, damping: 15 };

type Vec3 = readonly [number, number, number];
const ZERO: Vec3 = [0, 0, 0];

interface LayerSpec {
  /** Profundidad en reposo, en px (positivo = hacia la cámara). */
  z: number;
  /** Avance en Z al terminar la pista de scroll. */
  dz: number;
  /** Subida al terminar la pista, en fracciones de la altura del escenario. */
  lift: number;
  /** Rotación en reposo (X, Y, Z) en grados. */
  rot?: Vec3;
  /** Rotación añadida al terminar la pista. */
  spin?: Vec3;
  /** Cuánto se orienta la cara de la capa con el giro del escenario (0 = siempre de frente). */
  follow?: number;
  enter?: boolean;
}

/**
 * Capas del escenario por `data-layer`. El primer plano sube 3 veces más rápido que el
 * fondo (`lift` 0.6 frente a 0.2) y además avanza hacia la cámara mientras el fondo retrocede.
 * Con `z` 0 y sin `rot`, título y copy quedan idénticos al layout del servidor.
 */
const LAYERS: Record<string, LayerSpec> = {
  ambient: { z: -600, dz: -120, lift: 0.06, follow: 0 },
  'float-ask': { z: -300, dz: -180, lift: 0.2, rot: [6, 14, -6], spin: [18, -30, 16], enter: true },
  'float-reply': { z: -360, dz: -160, lift: 0.2, rot: [-4, -16, 5], spin: [-14, 26, -18], enter: true },
  'float-speed': { z: -260, dz: -200, lift: 0.2, rot: [3, -20, 3], spin: [16, 34, 10], enter: true },
  'float-typing': { z: -420, dz: -140, lift: 0.2, rot: [-6, 18, 4], spin: [-20, -28, -12], enter: true },
  'float-lead': { z: -320, dz: -180, lift: 0.2, rot: [5, -12, -4], spin: [22, 20, -16], enter: true },
  'float-booking': { z: -380, dz: -150, lift: 0.2, rot: [-3, 10, 6], spin: [-12, -32, 20], enter: true },
  title: { z: 0, dz: -280, lift: 0.3, spin: [14, 0, 0], follow: 0.6 },
  copy: { z: 0, dz: 0, lift: 0.14, follow: 0 },
  phone: { z: 0, dz: 110, lift: 0.12, rot: [8, -16, 2], spin: [-12, 36, -4] },
  'card-time': { z: 200, dz: 420, lift: 0.6, rot: [-6, 18, -5], spin: [26, -44, 18], enter: true },
  'card-probability': { z: 140, dz: 380, lift: 0.6, rot: [4, -22, 6], spin: [-20, 48, -22], enter: true },
  'card-sales': { z: 90, dz: 360, lift: 0.6, rot: [-4, -14, -3], spin: [30, 30, 14], enter: true },
};

type Mode = 'scene' | 'flow' | 'off';

interface Spring {
  value: number;
  velocity: number;
  target: number;
}

interface Layer {
  name: string;
  el: HTMLElement;
  spec: LayerSpec;
  glint: HTMLElement | null;
  /** Centro de layout respecto al centro del escenario. */
  ox: number;
  oy: number;
  /** Posición en el mundo cuya proyección en reposo cae exactamente en (ox, oy). */
  wx: number;
  wy: number;
}

const spring = (): Spring => ({ value: 0, velocity: 0, target: 0 });

function stepSpring(s: Spring, dt: number, { stiffness, damping }: SpringConfig): void {
  const force = -stiffness * (s.value - s.target) - damping * s.velocity;
  s.velocity += force * dt;
  s.value += s.velocity * dt;
}

const settled = (s: Spring) => Math.abs(s.value - s.target) < REST && Math.abs(s.velocity) < REST;
const clamp = (v: number, min = 0, max = 1) => Math.min(max, Math.max(min, v));
const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
const easeOut = (t: number) => 1 - Math.pow(1 - t, 3);
const n = (v: number, digits = 3) => v.toFixed(digits);

/** Centro de `el` en coordenadas de layout de `root`; ignora transforms. */
function layoutCenter(el: HTMLElement, root: HTMLElement): { x: number; y: number } | null {
  let x = el.offsetWidth / 2;
  let y = el.offsetHeight / 2;
  let node: HTMLElement | null = el;
  while (node && node !== root) {
    x += node.offsetLeft;
    y += node.offsetTop;
    node = node.offsetParent as HTMLElement | null;
  }
  return node === root ? { x, y } : null;
}

/**
 * Escenario 3D del hero.
 *
 * En escritorio el hero es una pista alta con un escenario `sticky`. Cada `[data-layer]`
 * es una capa con profundidad propia: una cámara compartida la proyecta en perspectiva,
 * el cursor gira todo el escenario con un resorte y el scroll empuja cada capa en Z, la
 * sube a su velocidad y la rota en los tres ejes. Solo se escriben `transform` y `opacity`.
 * En móvil solo endereza el teléfono al entrar en pantalla; con "reducir movimiento" no
 * transforma nada. Emite `heroInView` con `true`/`false` cada vez que el hero entra o sale
 * de pantalla (el componente pausa la demo del chat cuando no se ve).
 */
@Directive({
  selector: '[appHeroDepth]',
  standalone: true,
})
export class HeroDepthDirective implements AfterViewInit, OnDestroy {
  @Output() readonly heroInView = new EventEmitter<boolean>();

  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly zone = inject(NgZone);
  private readonly isBrowser = isPlatformBrowser(inject(PLATFORM_ID));

  private readonly progress = spring();
  private readonly pointerX = spring();
  private readonly pointerY = spring();
  private readonly intro = spring();

  private stage: HTMLElement | null = null;
  private stageHeight = 0;
  private layers: Layer[] = [];
  private sheen: HTMLElement | null = null;
  private edge: HTMLElement | null = null;
  private cue: HTMLElement | null = null;
  private mode: Mode = 'off';
  private pointerEnabled = false;
  private visible = false;
  private revealed = false;
  private needsSnap = true;
  private dirty = true;
  private raf = 0;
  private lastFrame = 0;
  private observer?: IntersectionObserver;
  private resizeObserver?: ResizeObserver;
  private cleanups: Array<() => void> = [];

  ngAfterViewInit(): void {
    if (!this.isBrowser) {
      return;
    }
    const host = this.host.nativeElement;
    this.stage = host.firstElementChild as HTMLElement | null;
    this.sheen = host.querySelector<HTMLElement>('[data-hero="sheen"]');
    this.edge = host.querySelector<HTMLElement>('[data-hero="edge"]');
    this.cue = host.querySelector<HTMLElement>('[data-hero="cue"]');
    this.layers = Array.from(host.querySelectorAll<HTMLElement>('[data-layer]')).flatMap((el) => {
      const name = el.dataset['layer'] ?? '';
      const spec = LAYERS[name];
      return spec
        ? [{ name, el, spec, glint: el.querySelector<HTMLElement>('.glint-band'), ox: 0, oy: 0, wx: 0, wy: 0 }]
        : [];
    });

    if (typeof IntersectionObserver === 'undefined') {
      this.visible = true;
      this.reveal();
    } else {
      this.observer = new IntersectionObserver((entries) => {
        const visible = entries.some((e) => e.isIntersecting);
        if (visible === this.visible && this.revealed) {
          return;
        }
        this.visible = visible;
        /* Fuera de pantalla, las animaciones infinitas (auras, blobs, bob) se pausan vía CSS. */
        host.classList.toggle('is-offstage', !visible);
        if (visible) {
          this.reveal();
          this.requestFrame();
        } else if (this.revealed) {
          this.zone.run(() => this.heroInView.emit(false));
        }
      });
      this.observer.observe(host);
    }

    const sceneQuery = window.matchMedia(SCENE_QUERY);
    const pointerQuery = window.matchMedia(POINTER_QUERY);
    const reducedQuery = window.matchMedia(REDUCED_QUERY);
    const sync = () => {
      this.pointerEnabled = pointerQuery.matches;
      const mode: Mode = reducedQuery.matches ? 'off' : sceneQuery.matches ? 'scene' : 'flow';
      this.setMode(mode);
    };

    this.zone.runOutsideAngular(() => {
      const onMove = (event: PointerEvent) => {
        if (!this.pointerEnabled || this.mode !== 'scene' || event.pointerType !== 'mouse') {
          return;
        }
        this.pointerX.target = clamp((event.clientX / window.innerWidth) * 2 - 1, -1, 1);
        this.pointerY.target = clamp((event.clientY / window.innerHeight) * 2 - 1, -1, 1);
        this.requestFrame();
      };
      const onLeave = () => {
        this.pointerX.target = 0;
        this.pointerY.target = 0;
        this.requestFrame();
      };
      const root = document.documentElement;
      window.addEventListener('pointermove', onMove, { passive: true });
      root.addEventListener('pointerleave', onLeave, { passive: true });
      window.addEventListener('blur', onLeave);
      for (const mq of [sceneQuery, pointerQuery, reducedQuery]) {
        mq.addEventListener('change', sync);
        this.cleanups.push(() => mq.removeEventListener('change', sync));
      }
      this.cleanups.push(
        () => window.removeEventListener('pointermove', onMove),
        () => root.removeEventListener('pointerleave', onLeave),
        () => window.removeEventListener('blur', onLeave),
      );

      if (typeof ResizeObserver !== 'undefined') {
        this.resizeObserver = new ResizeObserver(() => {
          this.measure();
          this.needsSnap = true;
          this.requestFrame();
        });
        this.resizeObserver.observe(host);
        this.layers.forEach((layer) => this.resizeObserver?.observe(layer.el));
      }
    });

    sync();
  }

  ngOnDestroy(): void {
    this.observer?.disconnect();
    this.resizeObserver?.disconnect();
    this.cleanups.forEach((fn) => fn());
    this.cleanups = [];
    if (this.raf) {
      cancelAnimationFrame(this.raf);
      this.raf = 0;
    }
  }

  private reveal(): void {
    if (!this.revealed) {
      this.revealed = true;
      this.host.nativeElement.classList.add('is-inview');
    }
    this.zone.run(() => this.heroInView.emit(true));
  }

  private setMode(mode: Mode): void {
    if (mode === this.mode) {
      return;
    }
    this.mode = mode;
    this.clearStyles();
    for (const s of [this.progress, this.pointerX, this.pointerY, this.intro]) {
      s.value = s.velocity = s.target = 0;
    }
    this.needsSnap = true;
    this.measure();
    if (mode === 'off') {
      if (this.raf) {
        cancelAnimationFrame(this.raf);
        this.raf = 0;
      }
      return;
    }
    this.requestFrame();
  }

  private measure(): void {
    const stage = this.stage;
    if (!stage) {
      return;
    }
    this.stageHeight = stage.offsetHeight;
    const cx = stage.offsetWidth / 2;
    const cy = this.stageHeight / 2;
    for (const layer of this.layers) {
      const c = layoutCenter(layer.el, stage);
      layer.ox = c ? c.x - cx : 0;
      layer.oy = c ? c.y - cy : 0;
      const k = (CAMERA - layer.spec.z) / CAMERA;
      layer.wx = layer.ox * k;
      layer.wy = layer.oy * k;
    }
  }

  private clearStyles(): void {
    const els = [
      ...this.layers.map((l) => l.el),
      ...this.layers.map((l) => l.glint),
      this.sheen,
      this.edge,
      this.cue,
    ].filter((el): el is HTMLElement => !!el);
    for (const el of els) {
      el.style.removeProperty('transform');
      el.style.removeProperty('opacity');
    }
    this.host.nativeElement.classList.remove('is-scene');
  }

  private requestFrame(): void {
    if (!this.raf && this.mode !== 'off' && this.visible) {
      this.lastFrame = performance.now();
      this.zone.runOutsideAngular(() => {
        this.raf = requestAnimationFrame(this.frame);
      });
    }
  }

  private readonly frame = (now: number): void => {
    this.raf = 0;
    if (this.mode === 'off' || !this.visible) {
      return;
    }
    const dt = clamp((now - this.lastFrame) / 1000, 1 / 240, 1 / 30);
    this.lastFrame = now;

    this.progress.target = this.readProgress();
    /* Si Lenis ya suaviza el scroll, el progreso sigue 1:1 para no acumular dos inercias. */
    if (this.needsSnap || document.documentElement.classList.contains('lenis')) {
      this.progress.value = this.progress.target;
      this.progress.velocity = 0;
    }
    if (this.mode === 'scene') {
      this.intro.target = 1;
    }

    const springs: Array<[Spring, SpringConfig]> = [
      [this.progress, SCROLL_SPRING],
      [this.pointerX, POINTER_SPRING],
      [this.pointerY, POINTER_SPRING],
      [this.intro, POINTER_SPRING],
    ];
    const idle = !this.needsSnap && !this.dirty && springs.every(([s]) => settled(s));
    springs.forEach(([s, config]) => stepSpring(s, dt, config));
    this.needsSnap = false;
    this.dirty = false;

    if (!idle) {
      if (this.mode === 'scene') {
        this.paintScene(this.progress.value);
      } else {
        this.paintFlow(this.progress.value);
      }
    }

    // No hay listener de `scroll`: mientras el hero es visible se lee su posición cada frame.
    this.raf = requestAnimationFrame(this.frame);
  };

  private readProgress(): number {
    const host = this.host.nativeElement;
    const vh = window.innerHeight || 1;
    if (this.mode === 'scene') {
      const rect = host.getBoundingClientRect();
      const travel = rect.height - (this.stage?.offsetHeight ?? vh);
      return travel > 0 ? clamp(-rect.top / travel) : 0;
    }
    const phone = this.layers.find((l) => l.name === 'phone')?.el;
    if (!phone) {
      return 1;
    }
    return clamp((vh - phone.getBoundingClientRect().top) / (vh * 0.65));
  }

  private paintScene(p: number): void {
    const yaw = this.pointerX.value * SCENE_YAW;
    const pitch = -this.pointerY.value * SCENE_PITCH;
    const cosY = Math.cos(yaw * DEG);
    const sinY = Math.sin(yaw * DEG);
    const cosP = Math.cos(pitch * DEG);
    const sinP = Math.sin(pitch * DEG);
    const arrival = 1 - this.intro.value;

    for (const layer of this.layers) {
      const { spec } = layer;
      const z = spec.z + spec.dz * p + (spec.enter ? arrival * ENTER_DEPTH : 0);

      // Cada capa pivota sobre su centro: el giro solo desplaza en proporción a la profundidad,
      // así lo que está en z = 0 (copy, CTA) no se mueve y el resto hace parallax.
      const x1 = layer.wx + z * sinY;
      const y1 = layer.wy - z * sinP;
      const z1 = z * cosY * cosP;
      const scale = CAMERA / Math.max(CAMERA * 0.25, CAMERA - z1);
      const tx = x1 * scale - layer.ox;
      const ty = y1 * scale - layer.oy - spec.lift * this.stageHeight * p;

      const rot = spec.rot ?? ZERO;
      const spin = spec.spin ?? ZERO;
      const follow = spec.follow ?? 1;
      const rx = rot[0] + spin[0] * p + pitch * follow;
      const ry = rot[1] + spin[1] * p + yaw * follow;
      const rz = rot[2] + spin[2] * p;

      layer.el.style.transform =
        `perspective(${CAMERA}px) translate3d(${n(tx, 2)}px, ${n(ty, 2)}px, 0) scale(${n(scale, 4)}) ` +
        `rotateX(${n(rx)}deg) rotateY(${n(ry)}deg) rotateZ(${n(rz)}deg)`;

      if (layer.glint) {
        this.paintGlint(layer.glint, rx, ry);
      }
      if (layer.name === 'phone') {
        this.paintGlass(rx, ry);
      }
    }

    if (this.cue) {
      this.cue.style.opacity = n(1 - clamp(p / 0.08));
    }
    this.host.nativeElement.classList.add('is-scene');
  }

  /** El destello cruza la tarjeta según su giro en Y y se intensifica cuanto más inclinada está. */
  private paintGlint(band: HTMLElement, rx: number, ry: number): void {
    const across = lerp(-140, 140, clamp((ry + 45) / 90));
    const down = lerp(-18, 18, clamp((rx + 30) / 60));
    band.style.transform = `translate3d(${n(across, 1)}%, ${n(down, 1)}%, 0)`;
    band.style.opacity = n(0.12 + 0.7 * clamp(Math.abs(ry) / 35 + Math.abs(rx) / 50));
  }

  /** Reflejo de la pantalla y filo del marco siguen el ángulo del teléfono. */
  private paintGlass(rx: number, ry: number): void {
    const tilt = clamp(Math.abs(rx) / 30 + Math.abs(ry) / 40);
    if (this.sheen) {
      this.sheen.style.transform = `translate3d(${n(ry * -1.6, 2)}%, ${n(rx * 1.1, 2)}%, 0)`;
      this.sheen.style.opacity = n(0.45 + tilt * 0.55);
    }
    if (this.edge) {
      this.edge.style.opacity = n(0.25 + tilt * 0.75);
    }
  }

  private paintFlow(p: number): void {
    const phone = this.layers.find((l) => l.name === 'phone')?.el;
    if (!phone) {
      return;
    }
    const e = easeOut(p);
    phone.style.transform =
      `perspective(1400px) translate3d(0, ${n((1 - e) * 24, 2)}px, 0) ` +
      `rotateX(${n((1 - e) * 18)}deg) scale(${n(lerp(1.05, 1, e), 4)})`;
  }
}
