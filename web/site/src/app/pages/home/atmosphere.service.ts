import { Injectable } from '@angular/core';

/** Color lineal (0..1) que GSAP puede interpolar campo a campo y Three leer tal cual. */
export interface Rgb {
  r: number;
  g: number;
  b: number;
}

/** Todo lo que cambia al "entrar en otra dimensión". Son números planos para poder tweenearlos. */
export interface AtmosphereState {
  color1: Rgb;
  color2: Rgb;
  background: Rgb;
  fogColor: Rgb;
  fogDensity: number;
  bloomStrength: number;
  bloomRadius: number;
  cameraZ: number;
  /** 0 = tema claro, 1 = "Deep Tech". Controla la intensidad de las partículas. */
  dark: number;
}

export interface AtmospherePreset {
  color1: string;
  color2: string;
  background: string;
  fog: string;
  fogDensity: number;
  bloomStrength: number;
  bloomRadius: number;
  cameraZ: number;
  dark: boolean;
}

/**
 * Paleta por sección. Los claros se apoyan en el crema del sitio; los oscuros ("Deep Tech") usan el
 * negro del panel con naranja y verde WhatsApp como luces de neón.
 */
export const ATMOSPHERES: Record<string, AtmospherePreset> = {
  hero: {
    color1: '#ffd9bf',
    color2: '#d8f6e4',
    background: '#fbfbfd',
    fog: '#fbfbfd',
    fogDensity: 0.085,
    bloomStrength: 0.12,
    bloomRadius: 0.2,
    cameraZ: 7,
    dark: false,
  },
  problem: {
    color1: '#ffc9a3',
    color2: '#ffe3d8',
    background: '#fff8f3',
    fog: '#fff4ea',
    fogDensity: 0.1,
    bloomStrength: 0.22,
    bloomRadius: 0.3,
    cameraZ: 6.2,
    dark: false,
  },
  features: {
    color1: '#2a1309',
    color2: '#072619',
    background: '#05080d',
    fog: '#05080d',
    fogDensity: 0.15,
    bloomStrength: 1.15,
    bloomRadius: 0.65,
    cameraZ: 3.6,
    dark: true,
  },
  panel: {
    color1: '#0a2e20',
    color2: '#0a2130',
    background: '#060b11',
    fog: '#060b11',
    fogDensity: 0.13,
    bloomStrength: 0.95,
    bloomRadius: 0.55,
    cameraZ: 3,
    dark: true,
  },
  industries: {
    color1: '#ffe2cc',
    color2: '#e3ecff',
    background: '#fbfbfd',
    fog: '#fbfbfd',
    fogDensity: 0.09,
    bloomStrength: 0.16,
    bloomRadius: 0.25,
    cameraZ: 5.6,
    dark: false,
  },
  web: {
    color1: '#c9d8ff',
    color2: '#ffe6d6',
    background: '#f4f7ff',
    fog: '#f4f7ff',
    fogDensity: 0.09,
    bloomStrength: 0.18,
    bloomRadius: 0.3,
    cameraZ: 5,
    dark: false,
  },
  how: {
    color1: '#ffd0a8',
    color2: '#d8f6e4',
    background: '#fff8f1',
    fog: '#fff8f1',
    fogDensity: 0.1,
    bloomStrength: 0.2,
    bloomRadius: 0.3,
    cameraZ: 4.6,
    dark: false,
  },
  compare: {
    color1: '#ffd4b3',
    color2: '#d4f5e0',
    background: '#fbfbfd',
    fog: '#fbfbfd',
    fogDensity: 0.09,
    bloomStrength: 0.15,
    bloomRadius: 0.25,
    cameraZ: 4.3,
    dark: false,
  },
  close: {
    color1: '#ffb27a',
    color2: '#ffe0c9',
    background: '#fff4ea',
    fog: '#fff2e6',
    fogDensity: 0.11,
    bloomStrength: 0.34,
    bloomRadius: 0.4,
    cameraZ: 3.8,
    dark: false,
  },
};

/** sRGB hex → lineal, el espacio en el que Three mezcla y en el que el OutputPass vuelve a codificar. */
export function hexToLinear(hex: string): Rgb {
  const value = parseInt(hex.replace('#', ''), 16);
  const channel = (c: number) => {
    const s = c / 255;
    return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  return {
    r: channel((value >> 16) & 255),
    g: channel((value >> 8) & 255),
    b: channel(value & 255),
  };
}

export function presetToState(preset: AtmospherePreset): AtmosphereState {
  return {
    color1: hexToLinear(preset.color1),
    color2: hexToLinear(preset.color2),
    background: hexToLinear(preset.background),
    fogColor: hexToLinear(preset.fog),
    fogDensity: preset.fogDensity,
    bloomStrength: preset.bloomStrength,
    bloomRadius: preset.bloomRadius,
    cameraZ: preset.cameraZ,
    dark: preset.dark ? 1 : 0,
  };
}

/**
 * Estado vivo compartido entre el scroll (que lo escribe con GSAP) y el lienzo WebGL (que lo lee
 * en cada frame). Vive solo mientras el home está montado.
 */
@Injectable()
export class AtmosphereService {
  readonly state: AtmosphereState = presetToState(ATMOSPHERES['hero']);
  /** Puntero normalizado (-1..1) ya suavizado por quien lo escribe. */
  readonly pointer = { x: 0, y: 0 };
  /** Sube cada vez que el scroll cambia el estado; el lienzo lo usa para no repintar en vano. */
  version = 0;

  touch(): void {
    this.version++;
  }
}
