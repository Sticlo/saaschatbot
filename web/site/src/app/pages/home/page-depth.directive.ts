import { isPlatformBrowser } from '@angular/common';
import { AfterViewInit, Directive, ElementRef, NgZone, OnDestroy, PLATFORM_ID, inject } from '@angular/core';

const REDUCED_QUERY = '(prefers-reduced-motion: reduce)';
const TILT_QUERY = '(hover: hover) and (pointer: fine)';
const SCENE_QUERY = '(min-width: 901px)';
const REST = 0.01;

interface SpringConfig {
  stiffness: number;
  damping: number;
}

/** El scroll suavizado da inercia al parallax sin quedarse atrás del dedo/rueda. */
const SCROLL_SPRING: SpringConfig = { stiffness: 120, damping: 22 };
const TILT_SPRING: SpringConfig = { stiffness: 170, damping: 20 };
const TILT_MAX = 7;
/** Cuántas palabras están a medio revelar a la vez. */
const WORD_WINDOW = 2.5;

interface Spring {
  value: number;
  velocity: number;
  target: number;
}

interface Drift {
  el: HTMLElement;
  /** Fracción de viewport que se desplaza por viewport de scroll (+ = primer plano, - = fondo). */
  speed: number;
  /** Grados de giro por viewport de scroll. */
  spin: number;
  glow: boolean;
  base: number;
  hidden: boolean;
}

interface WordGroup {
  el: HTMLElement;
  words: HTMLElement[];
  last: number[];
  top: number;
}

interface DepthScene {
  el: HTMLElement;
  centre: number;
}

interface TiltCard {
  el: HTMLElement;
  light: HTMLElement | null;
  rx: Spring;
  ry: Spring;
  lx: Spring;
  ly: Spring;
  hover: Spring;
  /** Grados máximos de inclinación; 0 deja solo el foco de luz (filas editoriales). */
  max: number;
  left: number;
  top: number;
  width: number;
  height: number;
}

const spring = (value = 0): Spring => ({ value, velocity: 0, target: value });

function stepSpring(s: Spring, dt: number, { stiffness, damping }: SpringConfig): void {
  const force = -stiffness * (s.value - s.target) - damping * s.velocity;
  s.velocity += force * dt;
  s.value += s.velocity * dt;
}

const settled = (s: Spring, rest = REST) => Math.abs(s.value - s.target) < rest && Math.abs(s.velocity) < rest;
const clamp = (v: number, min = 0, max = 1) => Math.min(max, Math.max(min, v));
const n = (v: number, digits = 3) => v.toFixed(digits);

/** Top de layout en el documento; ignora transforms. */
function layoutTop(el: HTMLElement): number {
  let y = 0;
  let node: HTMLElement | null = el;
  while (node) {
    y += node.offsetTop;
    node = node.offsetParent as HTMLElement | null;
  }
  return y;
}

/**
 * Profundidad del resto de la landing.
 *
 * - `[data-speed]`: parallax vertical con inercia (y giro opcional con `data-spin`).
 *   `data-glow` además hace que el elemento brille más cerca del centro del viewport.
 * - `[data-words]`: revela sus `.reveal-word` palabra a palabra según el scroll (blur → nítido).
 * - `[data-tilt]`: inclinación 3D con resorte y un foco de luz (`.bento-light`) que sigue al cursor.
 * - `[data-reveal]`: recibe `is-revealed` la primera vez que entra en pantalla.
 *
 * Con "reducir movimiento" solo se aplican las revelaciones, sin desplazamientos.
 */
@Directive({
  selector: '[appPageDepth]',
  standalone: true,
})
export class PageDepthDirective implements AfterViewInit, OnDestroy {
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly zone = inject(NgZone);
  private readonly isBrowser = isPlatformBrowser(inject(PLATFORM_ID));

  private readonly scroll = spring();
  private drifts: Drift[] = [];
  private wordGroups: WordGroup[] = [];
  private scenes: DepthScene[] = [];
  private cards: TiltCard[] = [];
  private motion = false;
  private tiltEnabled = false;
  private scenesEnabled = false;
  private needsSnap = true;
  private raf = 0;
  private lastFrame = 0;
  private revealObserver?: IntersectionObserver;
  private resizeObserver?: ResizeObserver;
  private cleanups: Array<() => void> = [];

  ngAfterViewInit(): void {
    if (!this.isBrowser) {
      return;
    }
    const host = this.host.nativeElement;
    const all = <T extends HTMLElement>(sel: string) => Array.from(host.querySelectorAll<T>(sel));

    this.drifts = all('[data-speed]').map((el) => ({
      el,
      speed: parseFloat(el.dataset['speed'] ?? '0') || 0,
      spin: parseFloat(el.dataset['spin'] ?? '0') || 0,
      glow: el.hasAttribute('data-glow'),
      base: 0,
      hidden: false,
    }));
    this.wordGroups = all('[data-words]').map((el) => {
      const words = Array.from(el.querySelectorAll<HTMLElement>('.reveal-word'));
      return { el, words, last: words.map(() => -1), top: 0 };
    });
    this.scenes = all('[data-scene]').map((el) => ({ el, centre: 0 }));
    this.cards = all('[data-tilt]').map((el) => ({
      el,
      light: el.querySelector<HTMLElement>('.bento-light'),
      rx: spring(),
      ry: spring(),
      lx: spring(),
      ly: spring(),
      hover: spring(),
      max: el.dataset['tilt'] === '' || el.dataset['tilt'] === undefined ? TILT_MAX : parseFloat(el.dataset['tilt']) || 0,
      left: 0,
      top: 0,
      width: 1,
      height: 1,
    }));

    this.setupReveal(all('[data-reveal]'));

    const reducedQuery = window.matchMedia(REDUCED_QUERY);
    const tiltQuery = window.matchMedia(TILT_QUERY);
    const sceneQuery = window.matchMedia(SCENE_QUERY);
    const sync = () => {
      this.tiltEnabled = tiltQuery.matches && !reducedQuery.matches;
      this.scenesEnabled = sceneQuery.matches && !reducedQuery.matches;
      this.setMotion(!reducedQuery.matches);
    };

    this.zone.runOutsideAngular(() => {
      const onScroll = () => this.requestFrame();
      window.addEventListener('scroll', onScroll, { passive: true });
      this.cleanups.push(() => window.removeEventListener('scroll', onScroll));

      for (const mq of [reducedQuery, tiltQuery, sceneQuery]) {
        mq.addEventListener('change', sync);
        this.cleanups.push(() => mq.removeEventListener('change', sync));
      }

      if (typeof ResizeObserver !== 'undefined') {
        this.resizeObserver = new ResizeObserver(() => {
          this.measure();
          this.requestFrame();
        });
        this.resizeObserver.observe(host);
      }

      this.cards.forEach((card) => this.bindTilt(card));
    });

    sync();
  }

  ngOnDestroy(): void {
    this.revealObserver?.disconnect();
    this.resizeObserver?.disconnect();
    this.cleanups.forEach((fn) => fn());
    this.cleanups = [];
    if (this.raf) {
      cancelAnimationFrame(this.raf);
      this.raf = 0;
    }
  }

  private setupReveal(targets: HTMLElement[]): void {
    if (typeof IntersectionObserver === 'undefined') {
      targets.forEach((el) => el.classList.add('is-revealed'));
      return;
    }
    this.revealObserver = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            entry.target.classList.add('is-revealed');
            this.revealObserver?.unobserve(entry.target);
          }
        }
      },
      { rootMargin: '0px 0px -12% 0px' },
    );
    targets.forEach((el) => this.revealObserver?.observe(el));
    this.host.nativeElement.classList.add('is-armed');
  }

  private setMotion(on: boolean): void {
    this.motion = on;
    if (!on) {
      this.clearStyles();
      return;
    }
    this.measure();
    this.needsSnap = true;
    this.requestFrame();
  }

  private measure(): void {
    for (const d of this.drifts) {
      d.base = layoutTop(d.el) + d.el.offsetHeight / 2;
    }
    for (const g of this.wordGroups) {
      g.top = layoutTop(g.el);
      g.last.fill(-1);
    }
    for (const scene of this.scenes) {
      scene.centre = layoutTop(scene.el) + scene.el.offsetHeight / 2;
    }
  }

  private clearStyles(): void {
    const els = [
      ...this.drifts.map((d) => d.el),
      ...this.wordGroups.flatMap((g) => g.words),
      ...this.scenes.map((scene) => scene.el),
      ...this.cards.flatMap((c) => [c.el, c.light]),
    ].filter((el): el is HTMLElement => !!el);
    for (const el of els) {
      el.style.removeProperty('transform');
      el.style.removeProperty('opacity');
      el.style.removeProperty('filter');
      el.style.removeProperty('visibility');
    }
    this.drifts.forEach((d) => (d.hidden = false));
    this.wordGroups.forEach((g) => g.last.fill(-1));
  }

  private bindTilt(card: TiltCard): void {
    const el = card.el;
    const onEnter = (event: PointerEvent) => {
      if (!this.tiltEnabled || event.pointerType !== 'mouse') {
        return;
      }
      const rect = el.getBoundingClientRect();
      card.left = rect.left;
      card.top = rect.top;
      card.width = el.offsetWidth || 1;
      card.height = el.offsetHeight || 1;
      const x = event.clientX - card.left;
      const y = event.clientY - card.top;
      // El foco nace donde entra el cursor en vez de viajar desde una esquina.
      card.lx.value = card.lx.target = x;
      card.ly.value = card.ly.target = y;
      card.hover.target = 1;
      this.aim(card, x, y);
    };
    const onMove = (event: PointerEvent) => {
      if (!this.tiltEnabled || event.pointerType !== 'mouse' || card.hover.target === 0) {
        return;
      }
      const rect = el.getBoundingClientRect();
      card.left = rect.left;
      card.top = rect.top;
      this.aim(card, event.clientX - card.left, event.clientY - card.top);
    };
    const onLeave = () => {
      card.rx.target = 0;
      card.ry.target = 0;
      card.hover.target = 0;
      this.requestFrame();
    };
    el.addEventListener('pointerenter', onEnter, { passive: true });
    el.addEventListener('pointermove', onMove, { passive: true });
    el.addEventListener('pointerleave', onLeave, { passive: true });
    this.cleanups.push(
      () => el.removeEventListener('pointerenter', onEnter),
      () => el.removeEventListener('pointermove', onMove),
      () => el.removeEventListener('pointerleave', onLeave),
    );
  }

  private aim(card: TiltCard, x: number, y: number): void {
    const nx = clamp(x / card.width) * 2 - 1;
    const ny = clamp(y / card.height) * 2 - 1;
    card.ry.target = nx * card.max;
    card.rx.target = -ny * card.max;
    card.lx.target = x;
    card.ly.target = y;
    this.requestFrame();
  }

  private requestFrame(): void {
    if (!this.raf && this.motion) {
      this.lastFrame = performance.now();
      this.zone.runOutsideAngular(() => {
        this.raf = requestAnimationFrame(this.frame);
      });
    }
  }

  private readonly frame = (now: number): void => {
    this.raf = 0;
    if (!this.motion) {
      return;
    }
    const dt = clamp((now - this.lastFrame) / 1000, 1 / 240, 1 / 30);
    this.lastFrame = now;
    let busy = false;

    this.scroll.target = window.scrollY;
    /* Con Lenis activo el scroll ya trae inercia: sumarle otro resorte lo haría flotar demasiado. */
    if (this.needsSnap || document.documentElement.classList.contains('lenis')) {
      this.scroll.value = this.scroll.target;
      this.scroll.velocity = 0;
      this.needsSnap = false;
    } else {
      stepSpring(this.scroll, dt, SCROLL_SPRING);
    }
    this.paintScroll(this.scroll.value);
    busy = !settled(this.scroll, 0.1);

    for (const card of this.cards) {
      const springs = [card.rx, card.ry, card.lx, card.ly, card.hover];
      if (springs.every((s) => settled(s))) {
        continue;
      }
      springs.forEach((s) => stepSpring(s, dt, TILT_SPRING));
      this.paintCard(card);
      busy = true;
    }

    if (busy) {
      this.raf = requestAnimationFrame(this.frame);
    }
  };

  private paintScroll(sy: number): void {
    const vh = window.innerHeight || 1;
    const centre = sy + vh / 2;

    for (const d of this.drifts) {
      const rel = (centre - d.base) / vh;
      /* Fuera de pantalla se ocultan del todo: una capa de 900 px con will-change sigue costando
         composición aunque no se vea; `visibility` la saca del pipeline sin reflow. */
      const offscreen = Math.abs(rel) > 1.8;
      if (offscreen !== d.hidden) {
        d.hidden = offscreen;
        d.el.style.visibility = offscreen ? 'hidden' : '';
      }
      if (offscreen) {
        continue;
      }
      const ty = -rel * vh * d.speed;
      d.el.style.transform = `translate3d(0, ${n(ty, 1)}px, 0) rotate(${n(rel * d.spin, 2)}deg)`;
      if (d.glow) {
        d.el.style.opacity = n(0.45 + 0.55 * (1 - clamp(Math.abs(rel))));
      }
    }

    for (const scene of this.scenes) {
      if (!this.scenesEnabled) {
        scene.el.style.removeProperty('transform');
        continue;
      }
      const rel = clamp((centre - scene.centre) / vh, -1.35, 1.35);
      const distance = Math.abs(rel);
      const z = -32 * Math.min(distance, 1);
      const ty = -rel * 14;
      const rx = rel * 1.55;
      const scale = 1 - Math.min(distance, 1) * 0.012;
      scene.el.style.transform =
        `perspective(1400px) translate3d(0, ${n(ty, 2)}px, ${n(z, 2)}px) ` +
        `rotateX(${n(rx, 3)}deg) scale(${n(scale, 4)})`;
    }

    for (const g of this.wordGroups) {
      const top = g.top - sy;
      const p = clamp((vh * 0.85 - top) / (vh * 0.45));
      const count = g.words.length;
      g.words.forEach((word, i) => {
        const t = clamp((p * (count + WORD_WINDOW) - i) / WORD_WINDOW);
        if (Math.abs(t - g.last[i]) < 0.004) {
          return;
        }
        g.last[i] = t;
        word.style.opacity = n(0.12 + 0.88 * t);
        word.style.filter = t >= 1 ? 'none' : `blur(${n((1 - t) * 10, 2)}px)`;
        word.style.transform = t >= 1 ? 'none' : `translate3d(0, ${n((1 - t) * 0.28, 3)}em, 0)`;
      });
    }
  }

  private paintCard(card: TiltCard): void {
    const lift = card.hover.value;
    if (card.max > 0) {
      card.el.style.transform =
        `perspective(900px) rotateX(${n(card.rx.value)}deg) rotateY(${n(card.ry.value)}deg) ` +
        `scale(${n(1 + 0.012 * lift, 4)})`;
    }
    if (card.light) {
      card.light.style.transform = `translate3d(${n(card.lx.value, 1)}px, ${n(card.ly.value, 1)}px, 0)`;
      card.light.style.opacity = n(clamp(lift));
    }
    /* El borde en gradiente (::before) lee estas variables para encenderse donde está el cursor. */
    card.el.style.setProperty('--mx', `${n(card.lx.value, 1)}px`);
    card.el.style.setProperty('--my', `${n(card.ly.value, 1)}px`);
    card.el.style.setProperty('--glow', n(clamp(lift)));
  }
}
