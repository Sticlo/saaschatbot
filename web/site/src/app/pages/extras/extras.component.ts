import { NgFor, NgIf } from '@angular/common';
import { ChangeDetectorRef, Component, HostListener, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import { CONTACT, whatsappUrl } from '../../core/contact';
import { ShellComponent } from '../../layout/shell/shell.component';

function formatCop(value: number): string {
  return new Intl.NumberFormat('es-CO', {
    style: 'currency',
    currency: 'COP',
    maximumFractionDigits: 0,
  }).format(value);
}

export interface ExtraService {
  slug: string;
  title: string;
  shortLabel: string;
  description: string;
  price: string;
  period?: string;
  tag?: string;
  featured?: boolean;
  accent: string;
  accentSoft: string;
  image: string;
  highlights: string[];
}

export interface AccessoryCategory {
  slug: string;
  title: string;
  image: string;
  images?: string[];
  items: string[];
}

@Component({
  selector: 'app-extras',
  imports: [ShellComponent, RouterLink, NgFor, NgIf],
  templateUrl: './extras.component.html',
  styleUrl: './extras.component.scss',
})
export class ExtrasComponent {
  private readonly cdr = inject(ChangeDetectorRef);

  readonly whatsappDisplay = CONTACT.whatsappDisplay;

  readonly heroStats = [
    { value: '5+', label: 'Servicios digitales' },
    { value: '24h', label: 'Respuesta cotización' },
    { value: 'COP', label: 'Precios claros' },
  ];

  readonly digitalServices: ExtraService[] = [
    {
      slug: 'paginas-web',
      title: 'Páginas web y landings',
      shortLabel: 'Web',
      description:
        'Sitio o landing profesional para tu negocio: rápida, clara y lista para captar clientes.',
      price: formatCop(600_000),
      tag: 'Desde',
      accent: '#ff6b00',
      accentSoft: '#fff0e6',
      image: '/images/paginas-web.jpg',
      highlights: [
        'Diseño moderno y responsive',
        'Formulario o botón a WhatsApp',
        'Entrega lista para publicar',
      ],
    },
    {
      slug: 'sistemas-negocio',
      title: 'Sistemas web para negocios',
      shortLabel: 'Negocio',
      description:
        'Sitio web con varias páginas y secciones — servicios, nosotros, contacto y más — pensado para posicionar mejor en Google. Ideal para empresas que ofrecen servicios.',
      price: formatCop(1_000_000),
      tag: 'Desde',
      accent: '#2563eb',
      accentSoft: '#eff6ff',
      image: '/images/sistemas-negocio.jpg',
      highlights: [
        'Más páginas = más palabras clave en Google',
        'Enfocado en empresas de servicios',
        'Formularios, WhatsApp y contenido optimizado',
      ],
    },
    {
      slug: 'sistemas-complejos',
      title: 'Sistemas a medida complejos',
      shortLabel: 'Enterprise',
      description:
        'Plataformas web robustas con varios módulos, roles, integraciones y escalabilidad.',
      price: formatCop(3_000_000),
      tag: 'Desde',
      featured: true,
      accent: '#7c3aed',
      accentSoft: '#f5f3ff',
      image: '/images/sistemas-complejos.jpg',
      highlights: [
        'Arquitectura pensada para crecer',
        'Múltiples usuarios y permisos',
        'Soporte en implementación',
      ],
    },
    {
      slug: 'seo',
      title: 'SEO',
      shortLabel: 'SEO',
      description:
        'Mejoramos tu visibilidad en Google para que más clientes te encuentren.',
      price: formatCop(800_000),
      period: '/mes',
      accent: '#059669',
      accentSoft: '#ecfdf5',
      image: '/images/seo.jpg',
      highlights: [
        'Optimización on-page',
        'Seguimiento de posiciones',
        'Recomendaciones mensuales',
      ],
    },
    {
      slug: 'contenido',
      title: 'Distribución de contenido',
      shortLabel: 'Contenido',
      description:
        'Publicamos y distribuimos tu contenido en los canales que uses para llegar a más gente.',
      price: formatCop(400_000),
      tag: 'Desde',
      accent: '#db2777',
      accentSoft: '#fdf2f8',
      image: '/images/contenido.jpg',
      highlights: [
        'Calendario editorial',
        'Adaptación por red o canal',
        'Coherencia con tu marca',
      ],
    },
  ];

  readonly accessoryCategories: AccessoryCategory[] = [
    {
      slug: 'carga',
      title: 'Carga y energía',
      image: '/images/accesorios-carga-1.jpg',
      images: [
        '/images/accesorios-carga-1.jpg',
        '/images/accesorios-carga-2.jpg',
        '/images/accesorios-carga-3.jpg',
      ],
      items: ['Cables USB', 'Carga rápida', 'Adaptadores y convertidores'],
    },
    {
      slug: 'audio',
      title: 'Audio',
      image: '/images/accesorios-audio-2.jpg',
      images: [
        '/images/accesorios-audio-1.jpg',
        '/images/accesorios-audio-2.jpg',
        '/images/accesorios-audio-3.jpg',
      ],
      items: ['Audífonos intraurales', 'Diademas', 'Micrófonos'],
    },
    {
      slug: 'perifericos',
      title: 'Periféricos',
      image: '/images/accesorios-perifericos-1.jpg',
      images: [
        '/images/accesorios-perifericos-1.jpg',
        '/images/accesorios-perifericos-2.jpg',
        '/images/accesorios-perifericos-3.jpg',
      ],
      items: ['Teclados', 'Mouse', 'Adaptadores USB y más'],
    },
  ];

  private readonly brokenImages = new Set<string>();
  private galleryTouchStartX = 0;

  galleryOpen = false;
  galleryTitle = '';
  galleryImages: string[] = [];
  galleryIndex = 0;

  @HostListener('document:keydown', ['$event'])
  onGalleryKeydown(event: KeyboardEvent): void {
    if (!this.galleryOpen) return;
    if (event.key === 'Escape') {
      this.closeGallery();
    } else if (event.key === 'ArrowRight') {
      this.nextGallery();
    } else if (event.key === 'ArrowLeft') {
      this.prevGallery();
    }
  }

  whatsappHref(topic?: string): string {
    const message = topic
      ? `Hola, me interesa cotizar: ${topic}`
      : 'Hola, me interesa una cotización de Extras Omitel (web, SEO, accesorios).';
    return whatsappUrl(message);
  }

  onImageError(src: string): void {
    this.brokenImages.add(src);
    this.cdr.markForCheck();
  }

  imageReady(src: string): boolean {
    return !this.brokenImages.has(src);
  }

  accessoryImages(cat: AccessoryCategory): string[] {
    return cat.images?.length ? cat.images : [cat.image];
  }

  accessoryImageReady(cat: AccessoryCategory): boolean {
    return this.accessoryImages(cat).some((src) => this.imageReady(src));
  }

  accessoryPreview(cat: AccessoryCategory): string {
    return this.accessoryImages(cat)[0] || cat.image;
  }

  openGallery(cat: AccessoryCategory): void {
    const images = this.accessoryImages(cat).filter((src) => this.imageReady(src));
    if (!images.length) return;
    this.galleryImages = images;
    this.galleryTitle = cat.title;
    this.galleryIndex = 0;
    this.galleryOpen = true;
    if (typeof document !== 'undefined') {
      document.body.style.overflow = 'hidden';
    }
  }

  closeGallery(): void {
    this.galleryOpen = false;
    if (typeof document !== 'undefined') {
      document.body.style.overflow = '';
    }
  }

  nextGallery(): void {
    if (!this.galleryImages.length) return;
    this.galleryIndex = (this.galleryIndex + 1) % this.galleryImages.length;
  }

  prevGallery(): void {
    if (!this.galleryImages.length) return;
    this.galleryIndex =
      (this.galleryIndex - 1 + this.galleryImages.length) % this.galleryImages.length;
  }

  onGalleryTouchStart(event: TouchEvent): void {
    this.galleryTouchStartX = event.changedTouches[0]?.clientX ?? 0;
  }

  onGalleryTouchEnd(event: TouchEvent): void {
    const endX = event.changedTouches[0]?.clientX ?? 0;
    const delta = endX - this.galleryTouchStartX;
    if (Math.abs(delta) < 40) return;
    if (delta < 0) {
      this.nextGallery();
    } else {
      this.prevGallery();
    }
  }
}
