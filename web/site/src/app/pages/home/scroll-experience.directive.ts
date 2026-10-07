import { isPlatformBrowser } from '@angular/common';
import { AfterViewInit, Directive, ElementRef, NgZone, OnDestroy, PLATFORM_ID, inject } from '@angular/core';
import type Lenis from 'lenis';

import { ATMOSPHERES, AtmosphereService, AtmosphereState, presetToState } from './atmosphere.service';
import { isLiteDevice } from './lite-mode';

type Gsap = typeof import('gsap').gsap;
type ScrollTriggerStatic = typeof import('gsap/ScrollTrigger').ScrollTrigger;

const REDUCED_QUERY = '(prefers-reduced-motion: reduce)';
/** Inercia "pesada": el scroll tarda en frenar, como una cámara con masa. */
const LENIS_LERP = 0.08;
const REFRESH_DEBOUNCE_MS = 200;
const PARALLAX_QUERY = '(min-width: 901px)';

/**
 * Orquesta el viaje por la landing:
 *
 * - Lenis suaviza el scroll nativo (los directivas de profundidad leen `window.scrollY` igual).
 * - Cada `[data-atmosphere]` interpola el estado del lienzo WebGL al entrar (colores, niebla, bloom, cámara).
 *   Las atmósferas oscuras además cambian el tema del documento para que texto y vidrio sigan legibles.
 * - `[data-mask-reveal]` parte el titular en palabras y las sube desde una máscara con `overflow: hidden`.
 * - `[data-parallax="n"]` desplaza la capa `n`% de su alto mientras su sección cruza el viewport.
 */
@Directive({
  selector: '[appScrollExperience]',
  standalone: true,
})
export class ScrollExperienceDirective implements AfterViewInit, OnDestroy {
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  private readonly zone = inject(NgZone);
  private readonly atmosphere = inject(AtmosphereService);
  private readonly isBrowser = isPlatformBrowser(inject(PLATFORM_ID));

  private gsap: Gsap | null = null;
  private lenis: Lenis | null = null;
  private tick: ((time: number) => void) | null = null;
  private destroyed = false;
  private darkCount = 0;
  private refreshTimer = 0;
  private resizeObserver?: ResizeObserver;
  private cleanups: Array<() => void> = [];
  /** Solo se destruye lo que esta instancia creó: otra instancia (HMR, navegación) puede convivir un instante. */
  private animations: Array<{ kill: () => void }> = [];

  ngAfterViewInit(): void {
    if (!this.isBrowser) {
      return;
    }
    this.zone.runOutsideAngular(() => {
      void this.boot();
    });
  }

  ngOnDestroy(): void {
    this.destroyed = true;
    window.clearTimeout(this.refreshTimer);
    this.resizeObserver?.disconnect();
    this.cleanups.forEach((fn) => fn());
    this.cleanups = [];
    this.animations.forEach((animation) => animation.kill());
    this.animations = [];
    if (this.gsap && this.tick) {
      this.gsap.ticker.remove(this.tick);
    }
    this.lenis?.destroy();
    this.lenis = null;
    if (this.darkCount > 0) {
      this.darkCount = 0;
      this.setDocumentTheme(false);
    }
    document.documentElement.classList.remove('atmos-live');
  }

  private async boot(): Promise<void> {
    const reduced = window.matchMedia(REDUCED_QUERY).matches;
    const smoothScroll = !reduced && !isLiteDevice();
    const [{ gsap }, { ScrollTrigger }, lenisModule] = await Promise.all([
      import('gsap'),
      import('gsap/ScrollTrigger'),
      smoothScroll ? import('lenis') : Promise.resolve(null),
    ]);
    if (this.destroyed) {
      return;
    }
    gsap.registerPlugin(ScrollTrigger);
    this.gsap = gsap;

    document.documentElement.classList.add('atmos-live');

    if (lenisModule) {
      const LenisCtor = lenisModule.default;
      const lenis = new LenisCtor({ lerp: LENIS_LERP, smoothWheel: true, anchors: true });
      lenis.on('scroll', ScrollTrigger.update);
      const tick = (time: number) => lenis.raf(time * 1000);
      gsap.ticker.add(tick);
      gsap.ticker.lagSmoothing(0);
      this.lenis = lenis;
      this.tick = tick;
    }

    this.buildAtmospheres(gsap, ScrollTrigger);
    this.buildMaskReveals(gsap, reduced);
    if (!reduced) {
      this.buildParallax(gsap);
    }

    const scheduleRefresh = () => {
      window.clearTimeout(this.refreshTimer);
      this.refreshTimer = window.setTimeout(() => ScrollTrigger.refresh(), REFRESH_DEBOUNCE_MS);
    };
    if (typeof ResizeObserver !== 'undefined') {
      this.resizeObserver = new ResizeObserver(scheduleRefresh);
      this.resizeObserver.observe(this.host.nativeElement);
    }
    window.addEventListener('load', scheduleRefresh, { once: true });
    this.cleanups.push(() => window.removeEventListener('load', scheduleRefresh));
    if (document.fonts?.ready) {
      void document.fonts.ready.then(scheduleRefresh);
    }
  }

  private buildAtmospheres(gsap: Gsap, ScrollTrigger: ScrollTriggerStatic): void {
    const sections = Array.from(this.host.nativeElement.querySelectorAll<HTMLElement>('[data-atmosphere]'));
    const state = this.atmosphere.state;
    let previous: AtmosphereState = presetToState(ATMOSPHERES['hero']);

    for (const section of sections) {
      const key = section.dataset['atmosphere'] ?? '';
      const preset = ATMOSPHERES[key];
      if (!preset) {
        continue;
      }
      const next = presetToState(preset);
      const from = previous;
      const timeline = gsap.timeline({
        defaults: { ease: 'none', immediateRender: false },
        scrollTrigger: {
          trigger: section,
          start: 'top 82%',
          end: 'top 22%',
          scrub: 0.9,
        },
        onUpdate: () => this.atmosphere.touch(),
      });
      this.animations.push(timeline);
      for (const channel of ['color1', 'color2', 'background', 'fogColor'] as const) {
        timeline.fromTo(state[channel], { ...from[channel] }, { ...next[channel] }, 0);
      }
      timeline.fromTo(
        state,
        {
          fogDensity: from.fogDensity,
          bloomStrength: from.bloomStrength,
          bloomRadius: from.bloomRadius,
          cameraZ: from.cameraZ,
          dark: from.dark,
        },
        {
          fogDensity: next.fogDensity,
          bloomStrength: next.bloomStrength,
          bloomRadius: next.bloomRadius,
          cameraZ: next.cameraZ,
          dark: next.dark,
        },
        0,
      );

      if (preset.dark) {
        this.animations.push(
          ScrollTrigger.create({
            trigger: section,
            start: 'top 55%',
            end: 'bottom 45%',
            onEnter: () => this.shiftDark(1),
            onEnterBack: () => this.shiftDark(1),
            onLeave: () => this.shiftDark(-1),
            onLeaveBack: () => this.shiftDark(-1),
          }),
        );
      }
      previous = next;
    }
  }

  private shiftDark(delta: number): void {
    this.darkCount = Math.max(0, this.darkCount + delta);
    this.setDocumentTheme(this.darkCount > 0);
  }

  private setDocumentTheme(dark: boolean): void {
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
  }

  private buildMaskReveals(gsap: Gsap, reduced: boolean): void {
    const headings = Array.from(this.host.nativeElement.querySelectorAll<HTMLElement>('[data-mask-reveal]'));
    for (const heading of headings) {
      const words = splitWords(heading);
      if (!words.length) {
        continue;
      }
      if (reduced) {
        continue;
      }
      this.animations.push(
        gsap.fromTo(
          words,
          { yPercent: 112 },
          {
            yPercent: 0,
            duration: 1.15,
            ease: 'expo.out',
            stagger: 0.045,
            scrollTrigger: { trigger: heading, start: 'top 86%', once: true },
          },
        ),
      );
    }
  }

  /**
   * `data-parallax="n"`: la capa recorre n% mientras su sección cruza el viewport. El porcentaje se
   * aplica sobre el menor entre la altura de la capa y la del viewport: un grid de 2400 px no debe
   * desplazarse 480 px y pisar el titular. En pantallas pequeñas no se activa.
   */
  private buildParallax(gsap: Gsap): void {
    if (!window.matchMedia(PARALLAX_QUERY).matches) {
      return;
    }
    const layers = Array.from(this.host.nativeElement.querySelectorAll<HTMLElement>('[data-parallax]'));
    for (const layer of layers) {
      const amount = parseFloat(layer.dataset['parallax'] ?? '0') || 0;
      if (!amount) {
        continue;
      }
      const section = layer.closest<HTMLElement>('section') ?? layer;
      const travel = () => (amount / 200) * Math.min(layer.offsetHeight || 0, window.innerHeight);
      this.animations.push(
        gsap.fromTo(
          layer,
          { y: () => -travel() },
          {
            y: () => travel(),
            ease: 'none',
            scrollTrigger: {
              trigger: section,
              start: 'top bottom',
              end: 'bottom top',
              scrub: true,
              invalidateOnRefresh: true,
            },
          },
        ),
      );
    }
  }
}

/**
 * Envuelve cada palabra en `.mask-word > .mask-word-inner` conservando `<br>` y otros elementos.
 * Devuelve los spans interiores, que son los que se animan.
 */
function splitWords(root: HTMLElement): HTMLElement[] {
  if (root.dataset['split'] === '1') {
    return Array.from(root.querySelectorAll<HTMLElement>('.mask-word-inner'));
  }
  const inners: HTMLElement[] = [];
  const wrap = (content: Node): HTMLElement => {
    const outer = document.createElement('span');
    outer.className = 'mask-word';
    const inner = document.createElement('span');
    inner.className = 'mask-word-inner';
    inner.appendChild(content);
    outer.appendChild(inner);
    inners.push(inner);
    return outer;
  };

  for (const node of Array.from(root.childNodes)) {
    if (node.nodeType === Node.TEXT_NODE) {
      const text = node.textContent ?? '';
      const tokens = text.split(/(\s+)/);
      const fragment = document.createDocumentFragment();
      for (const token of tokens) {
        if (!token) {
          continue;
        }
        if (/^\s+$/.test(token)) {
          fragment.appendChild(document.createTextNode(' '));
        } else {
          fragment.appendChild(wrap(document.createTextNode(token)));
        }
      }
      root.replaceChild(fragment, node);
    } else if (node.nodeType === Node.ELEMENT_NODE && (node as HTMLElement).tagName !== 'BR') {
      root.replaceChild(wrap(node.cloneNode(true)), node);
    }
  }
  root.dataset['split'] = '1';
  return inners;
}
