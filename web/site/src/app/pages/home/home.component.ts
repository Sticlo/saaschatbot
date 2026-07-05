import { AsyncPipe, NgFor, NgIf, isPlatformBrowser } from '@angular/common';
import {
  Component,
  OnDestroy,
  OnInit,
  PLATFORM_ID,
  inject,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import { whatsappUrl } from '../../core/contact';
import { environment } from '../../../environments/environment';
import { ShellComponent } from '../../layout/shell/shell.component';
import { SessionService } from '../../core/services/session.service';

interface Beat {
  step: string;
  title: string;
  line: string;
  detail: string;
}

interface Stat {
  value: string;
  label: string;
}

interface Feature {
  icon: string;
  title: string;
  line: string;
}

interface Industry {
  name: string;
  example: string;
}

interface CompareRow {
  without: string;
  with: string;
}

interface FaqItem {
  q: string;
  a: string;
}

interface WebOffering {
  icon: string;
  title: string;
  line: string;
  price: string;
  highlights: string[];
}

interface DemoChatMessage {
  direction: 'in' | 'out';
  time: string;
  text: string;
}

interface DemoScenario {
  id: string;
  vertical: string;
  contactName: string;
  contactInitial: string;
  contactRole: string;
  alertText: string;
  messages: DemoChatMessage[];
  insights: {
    timeSaved: string;
    sales: string;
    probabilityStart: number;
    probabilityEnd: number;
  };
}

@Component({
  selector: 'app-home',
  imports: [ShellComponent, RouterLink, NgIf, NgFor, AsyncPipe],
  templateUrl: './home.component.html',
  styleUrl: './home.component.scss',
})
export class HomeComponent implements OnInit, OnDestroy {
  private readonly session = inject(SessionService);
  private readonly platformId = inject(PLATFORM_ID);
  private readonly chatStorageKey = 'omitel.heroChatStatic';
  private readonly scenarioDurationMs = 9000;

  private chatTimers: ReturnType<typeof setTimeout>[] = [];
  private scriptIndex = 0;
  private scenarioIndex = 0;

  get panelUrl(): string {
    if (typeof window !== 'undefined') {
      return `${window.location.origin}${environment.panelUrl}`;
    }
    return environment.panelUrl;
  }
  readonly user$ = this.session.user$;

  readonly beats: Beat[] = [
    {
      step: '01',
      title: 'Conecta',
      line: 'Escanea un QR. Tu WhatsApp queda en el panel en minutos.',
      detail: 'Sin apps extra ni migrar chats. Tu número sigue en tu celular.',
    },
    {
      step: '02',
      title: 'Filtra',
      line: 'La IA responde lo básico y separa curiosos de compradores.',
      detail: 'Precios, horarios y dudas frecuentes — tú no repites lo mismo 50 veces.',
    },
    {
      step: '03',
      title: 'Cierra',
      line: 'Solo entras cuando alguien ya quiere comprar.',
      detail: 'El panel te avisa con «Interesado» y tú cierras la venta en persona.',
    },
  ];

  readonly stats: Stat[] = [
    { value: '24/7', label: 'IA respondiendo mientras duermes' },
    { value: '−70%', label: 'Menos tiempo en chats repetitivos' },
    { value: '1 panel', label: 'Todos tus chats en un solo lugar' },
    { value: '7 días', label: 'Prueba gratis, sin tarjeta' },
  ];

  readonly features: Feature[] = [
    {
      icon: 'ai',
      title: 'IA que suena humano',
      line: 'Responde en español colombiano, con el tono de tu negocio y respuestas cortas.',
    },
    {
      icon: 'filter',
      title: 'Interesados automáticos',
      line: 'Detecta quién quiere comprar, reservar o agendar y te avisa al instante.',
    },
    {
      icon: 'panel',
      title: 'Panel en tiempo real',
      line: 'Chats, mensajes y estado de conexión en una sola pantalla — desde el navegador.',
    },
    {
      icon: 'manual',
      title: 'Modo manual cuando quieras',
      line: 'Tomas el control de un chat con un clic. La IA se pausa solo ahí.',
    },
    {
      icon: 'shortcuts',
      title: 'Atajos y carnada',
      line: 'Mensajes listos para prospectar y responder más rápido a clientes calientes.',
    },
    {
      icon: 'appointments',
      title: 'Citas y calificación',
      line: 'Agenda visitas y califica leads sin perder el hilo en WhatsApp.',
    },
  ];

  readonly industries: Industry[] = [
    { name: 'Restaurantes', example: 'Reservas, menú y domicilios' },
    { name: 'Clínicas', example: 'Citas, horarios y especialidades' },
    { name: 'Inmobiliarias', example: 'Visitas, precios y disponibilidad' },
    { name: 'Talleres', example: 'Cotizaciones y agendamiento' },
    { name: 'Tiendas', example: 'Stock, envíos y apartados' },
    { name: 'Servicios', example: 'Consultas, propuestas y seguimiento' },
  ];

  readonly compareRows: CompareRow[] = [
    { without: 'Respondes lo mismo todo el día', with: 'La IA contesta preguntas frecuentes' },
    { without: 'Pierdes ventas en chats olvidados', with: 'Ningún mensaje queda sin respuesta' },
    { without: 'No sabes quién sí quiere comprar', with: 'Badge «Interesado» en el panel' },
    { without: 'WhatsApp mezclado con lo personal', with: 'Panel separado para tu negocio' },
  ];

  readonly faqs: FaqItem[] = [
    {
      q: '¿Necesito cambiar de número de WhatsApp?',
      a: 'No. Conectas el mismo número que ya usas escaneando un QR. Tus chats siguen en el celular.',
    },
    {
      q: '¿La IA responde sola a todos?',
      a: 'Tú decides chat por chat. Puedes activar IA global, marcar «Interesado» o poner un chat en modo manual.',
    },
    {
      q: '¿Funciona si ya tengo muchos chats?',
      a: 'Sí. Omitel sincroniza conversaciones nuevas. Los chats antiguos no se importan automáticamente al conectar.',
    },
    {
      q: '¿Qué pasa después de los 7 días gratis?',
      a: 'Eliges un plan desde $120.000/mes. Sin contratos — cancelas cuando quieras.',
    },
  ];

  readonly webOfferings: WebOffering[] = [
    {
      icon: 'landing',
      title: 'Páginas web y landings',
      line: 'Sitio profesional, rápido y listo para captar clientes desde Google o redes.',
      price: 'Desde $600.000',
      highlights: ['Diseño responsive', 'Botón a WhatsApp', 'Lista para publicar'],
    },
    {
      icon: 'app',
      title: 'Sistemas web para negocios',
      line: 'Web con varias páginas y secciones para mejorar tu SEO. Pensado para empresas de servicios.',
      price: 'Desde $1.000.000',
      highlights: ['Más páginas en Google', 'Empresas de servicios', 'WhatsApp y formularios'],
    },
    {
      icon: 'platform',
      title: 'Sistemas a medida',
      line: 'Plataformas completas con varios módulos, roles, permisos e integraciones.',
      price: 'Desde $3.000.000',
      highlights: ['Arquitectura escalable', 'Multi-usuario', 'Soporte en implementación'],
    },
  ];

  webQuoteHref(topic?: string): string {
    const message = topic
      ? `Hola, me interesa cotizar: ${topic}`
      : 'Hola, me interesa una cotización de desarrollo web (páginas o aplicaciones).';
    return whatsappUrl(message);
  }

  readonly scenarios: DemoScenario[] = [
    {
      id: 'restaurant',
      vertical: 'Restaurante',
      contactName: 'María',
      contactInitial: 'M',
      contactRole: 'Cliente',
      alertText: 'Interesado detectado · Reserva confirmada',
      insights: {
        timeSaved: '12 min',
        sales: '+$45.000',
        probabilityStart: 34,
        probabilityEnd: 94,
      },
      messages: [
        {
          direction: 'in',
          time: '10:41',
          text: '¿Cuánto sale el combo familiar?',
        },
        {
          direction: 'out',
          time: '10:42',
          text: 'Hola María, está en $45.000. ¿Reservamos?',
        },
        {
          direction: 'in',
          time: '10:43',
          text: 'Dale, somos 6. Hoy a las 7pm',
        },
      ],
    },
    {
      id: 'clinic',
      vertical: 'Clínica',
      contactName: 'Laura',
      contactInitial: 'L',
      contactRole: 'Paciente',
      alertText: 'Interesado detectado · Quiere agendar cita',
      insights: {
        timeSaved: '8 min',
        sales: '+1 cita',
        probabilityStart: 41,
        probabilityEnd: 91,
      },
      messages: [
        {
          direction: 'in',
          time: '11:08',
          text: '¿Tienen cita para dermatología mañana?',
        },
        {
          direction: 'out',
          time: '11:09',
          text: 'Hola Laura, sí, hay cupo a las 3pm. ¿Confirmamos?',
        },
        {
          direction: 'in',
          time: '11:10',
          text: 'Perfecto, voy con mi hija. Gracias',
        },
      ],
    },
    {
      id: 'real-estate',
      vertical: 'Inmobiliaria',
      contactName: 'Carlos',
      contactInitial: 'C',
      contactRole: 'Cliente',
      alertText: 'Interesado detectado · Visita agendada',
      insights: {
        timeSaved: '18 min',
        sales: '+$420M',
        probabilityStart: 28,
        probabilityEnd: 89,
      },
      messages: [
        {
          direction: 'in',
          time: '2:14',
          text: 'Busco apto de 3 hab en Chía, ¿cuánto pide?',
        },
        {
          direction: 'out',
          time: '2:15',
          text: 'Hola Carlos, tengo uno en $420M con parqueadero. ¿Te mando fotos?',
        },
        {
          direction: 'in',
          time: '2:16',
          text: 'Sí, me interesa. ¿Podemos verlo el sábado?',
        },
      ],
    },
    {
      id: 'workshop',
      vertical: 'Taller',
      contactName: 'Andrea',
      contactInitial: 'A',
      contactRole: 'Cliente',
      alertText: 'Interesado detectado · Servicio confirmado',
      insights: {
        timeSaved: '15 min',
        sales: '+$380.000',
        probabilityStart: 36,
        probabilityEnd: 93,
      },
      messages: [
        {
          direction: 'in',
          time: '4:22',
          text: '¿Cuánto cuesta cambio de frenos del Aveo?',
        },
        {
          direction: 'out',
          time: '4:23',
          text: 'Hola Andrea, queda en $380.000 con repuestos. ¿Agendamos?',
        },
        {
          direction: 'in',
          time: '4:24',
          text: 'Listo, llevo el carro mañana en la mañana',
        },
      ],
    },
    {
      id: 'store',
      vertical: 'Tienda',
      contactName: 'Diego',
      contactInitial: 'D',
      contactRole: 'Cliente',
      alertText: 'Interesado detectado · Compra inmediata',
      insights: {
        timeSaved: '6 min',
        sales: '+$89.000',
        probabilityStart: 52,
        probabilityEnd: 97,
      },
      messages: [
        {
          direction: 'in',
          time: '6:05',
          text: '¿Tienen la camisa M en azul? ¿Envían a Medellín?',
        },
        {
          direction: 'out',
          time: '6:06',
          text: 'Hola Diego, sí, $89.000 y envío en 2 días. ¿Te la aparto?',
        },
        {
          direction: 'in',
          time: '6:07',
          text: 'Dale, pago ya. Envíame el link',
        },
      ],
    },
  ];

  currentScenario: DemoScenario = this.scenarios[0];
  visibleMessages: DemoChatMessage[] = [];
  chatStatic = false;
  showTyping = false;
  typingDirection: 'in' | 'out' = 'in';
  showAlert = false;
  scenarioTransition = false;
  showInsightTime = false;
  showInsightSales = false;
  showInsightProbability = false;
  displayProbability = 0;
  probabilityTrend = '';
  insightPop: 'probability' | 'time' | 'sales' | null = null;

  private probabilityAnimToken = 0;

  ngOnInit(): void {
    if (!isPlatformBrowser(this.platformId)) {
      this.applyStaticMode(true);
      return;
    }

    const saved = localStorage.getItem(this.chatStorageKey);
    if (saved === '1') {
      this.applyStaticMode(true);
      return;
    }

    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      this.applyStaticMode(true);
      return;
    }

    this.startLiveChat();
  }

  ngOnDestroy(): void {
    this.clearChatTimers();
  }

  toggleChatMode(): void {
    if (this.chatStatic) {
      this.applyStaticMode(false);
      if (isPlatformBrowser(this.platformId)) {
        localStorage.setItem(this.chatStorageKey, '0');
      }
      this.startLiveChat();
      return;
    }

    this.applyStaticMode(true);
    if (isPlatformBrowser(this.platformId)) {
      localStorage.setItem(this.chatStorageKey, '1');
    }
  }

  private applyStaticMode(staticMode: boolean): void {
    this.chatStatic = staticMode;
    this.clearChatTimers();
    this.showTyping = false;
    this.scenarioTransition = false;
    this.resetInsights();
    this.currentScenario = this.scenarios[0];

    if (staticMode) {
      this.visibleMessages = [...this.currentScenario.messages];
      this.showAlert = true;
      this.showAllInsights();
      return;
    }

    this.visibleMessages = [];
    this.showAlert = false;
    this.scriptIndex = 0;
    this.scenarioIndex = 0;
  }

  private startLiveChat(): void {
    if (!isPlatformBrowser(this.platformId) || this.chatStatic) {
      return;
    }

    this.clearChatTimers();
    this.scenarioIndex = 0;
    this.playScenario(this.scenarioIndex);
  }

  private playScenario(index: number): void {
    if (this.chatStatic) {
      return;
    }

    this.clearChatTimers();
    this.scenarioIndex = index;
    this.currentScenario = this.scenarios[index];
    this.visibleMessages = [];
    this.showAlert = false;
    this.showTyping = false;
    this.scriptIndex = 0;
    this.resetInsights();
    this.scenarioTransition = true;

    this.schedule(() => {
      this.scenarioTransition = false;
    }, 320);

    this.schedule(() => {
      if (!this.chatStatic) {
        const next = (index + 1) % this.scenarios.length;
        this.playScenario(next);
      }
    }, this.scenarioDurationMs);

    this.runNextBeat(380);
  }

  private runNextBeat(initialDelay = 0): void {
    const script = this.currentScenario.messages;

    if (this.chatStatic || this.scriptIndex >= script.length) {
      if (!this.chatStatic && this.scriptIndex >= script.length) {
        this.schedule(() => {
          this.showAlert = true;
        }, 420);
      }
      return;
    }

    const next = script[this.scriptIndex];
    this.schedule(() => {
      this.showTyping = true;
      this.typingDirection = next.direction;

      this.schedule(() => {
        this.showTyping = false;
        this.visibleMessages = [...this.visibleMessages, next];
        this.scriptIndex += 1;
        this.updateInsightsForMessage(this.scriptIndex);
        this.runNextBeat(next.direction === 'out' ? 520 : 480);
      }, next.direction === 'out' ? 720 : 640);
    }, initialDelay);
  }

  private schedule(fn: () => void, ms: number): void {
    const id = setTimeout(fn, ms);
    this.chatTimers.push(id);
  }

  private clearChatTimers(): void {
    for (const id of this.chatTimers) {
      clearTimeout(id);
    }
    this.chatTimers = [];
  }

  private resetInsights(): void {
    this.showInsightTime = false;
    this.showInsightSales = false;
    this.showInsightProbability = false;
    this.displayProbability = 0;
    this.probabilityTrend = '';
    this.insightPop = null;
    this.probabilityAnimToken += 1;
  }

  private showAllInsights(): void {
    const { probabilityEnd } = this.currentScenario.insights;
    this.showInsightProbability = true;
    this.showInsightTime = true;
    this.showInsightSales = true;
    this.displayProbability = probabilityEnd;
    this.probabilityTrend = `↑ ${probabilityEnd}%`;
  }

  private updateInsightsForMessage(messageCount: number): void {
    const { insights } = this.currentScenario;
    const mid =
      insights.probabilityStart +
      Math.round((insights.probabilityEnd - insights.probabilityStart) * 0.55);

    if (messageCount === 1) {
      this.showInsightProbability = true;
      this.triggerInsightPop('probability');
      this.animateProbability(insights.probabilityStart);
      this.probabilityTrend = '';
      return;
    }

    if (messageCount === 2) {
      this.showInsightTime = true;
      this.triggerInsightPop('time');
      this.animateProbability(mid);
      this.probabilityTrend = `↑ ${mid - insights.probabilityStart}%`;
      return;
    }

    if (messageCount >= 3) {
      this.showInsightSales = true;
      this.triggerInsightPop('sales');
      this.animateProbability(insights.probabilityEnd);
      this.probabilityTrend = `↑ ${insights.probabilityEnd - insights.probabilityStart}%`;
    }
  }

  private triggerInsightPop(type: 'probability' | 'time' | 'sales'): void {
    this.insightPop = type;
    this.schedule(() => {
      if (this.insightPop === type) {
        this.insightPop = null;
      }
    }, 720);
  }

  private animateProbability(target: number): void {
    const token = ++this.probabilityAnimToken;
    const start = this.displayProbability;
    const diff = target - start;
    const steps = Math.max(8, Math.abs(diff));
    const stepMs = 36;
    let step = 0;

    const tick = (): void => {
      if (token !== this.probabilityAnimToken) {
        return;
      }

      step += 1;
      const progress = step / steps;
      const eased = 1 - (1 - progress) ** 3;
      this.displayProbability = Math.round(start + diff * eased);

      if (step < steps) {
        this.schedule(tick, stepMs);
      } else {
        this.displayProbability = target;
      }
    };

    tick();
  }
}
