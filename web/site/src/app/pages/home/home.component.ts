import { AsyncPipe, NgFor, NgIf } from '@angular/common';
import { Component, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import { environment } from '../../../environments/environment';
import { ShellComponent } from '../../layout/shell/shell.component';
import { SessionService } from '../../core/services/session.service';

interface PainPoint {
  emoji: string;
  title: string;
  text: string;
}

interface Outcome {
  value: string;
  label: string;
  detail: string;
}

interface Feature {
  tag: string;
  title: string;
  text: string;
}

interface Persona {
  name: string;
  example: string;
}

@Component({
  selector: 'app-home',
  imports: [ShellComponent, RouterLink, NgIf, NgFor, AsyncPipe],
  templateUrl: './home.component.html',
  styleUrl: './home.component.scss',
})
export class HomeComponent {
  private readonly session = inject(SessionService);

  readonly panelUrl = environment.panelUrl;
  readonly user$ = this.session.user$;

  readonly pains: PainPoint[] = [
    {
      emoji: '😵',
      title: '200 chats y no sabes por dónde empezar',
      text: 'Respondes lo mismo todo el día: precios, horario, ubicación… y los que sí quieren comprar se pierden entre el ruido.',
    },
    {
      emoji: '💸',
      title: 'Leads fríos que nunca vuelven',
      text: 'Te escribieron ayer, hoy ya compraron en otro lado. Sin seguimiento ordenado, la venta se escapa.',
    },
    {
      emoji: '⏰',
      title: 'Tu tiempo vale más que copiar y pegar',
      text: 'Quieres vender y atender bien, no vivir pegado al celular contestando lo básico.',
    },
  ];

  readonly outcomes: Outcome[] = [
    {
      value: 'Menos ruido',
      label: 'La IA filtra curiosos',
      detail: 'Responde dudas repetidas y solo te molesta cuando hay interés real.',
    },
    {
      value: 'Interesados',
      label: 'Pestaña lista para cerrar',
      detail: 'Entras al panel y ves quién pidió precio, reserva o quiere comprar hoy.',
    },
    {
      value: 'Más ventas',
      label: 'Tú cierras, la IA abre',
      detail: 'Omitel no reemplaza al vendedor — te devuelve tiempo para convertir.',
    },
  ];

  readonly features: Feature[] = [
    {
      tag: 'Panel',
      title: 'Todos tus chats en un solo lugar',
      text: 'WhatsApp conectado, conversaciones en tiempo real y control manual cuando quieres tomar un chat.',
    },
    {
      tag: 'IA qualify',
      title: 'Saluda y califica por ti',
      text: 'Responde al inicio con la info de tu negocio. Detecta intención de compra sin sonar a robot genérico.',
    },
    {
      tag: 'Interesados',
      title: 'Solo entras a quien quiere comprar',
      text: 'Pestaña dedicada con leads calientes. Dejas de revisar 150 conversaciones para encontrar 3 buenas.',
    },
    {
      tag: 'Tu negocio',
      title: 'Precios, horarios y tono tuyos',
      text: 'Configuras rubro, menú, políticas y cómo habla la IA — responde como tu marca, no como un template.',
    },
    {
      tag: 'Atajos',
      title: 'Menú, fotos y precios en un clic',
      text: 'Envía lo que el cliente pide sin reescribir lo mismo en cada chat.',
    },
    {
      tag: 'Prospección',
      title: 'Contactos fríos con control',
      text: 'Carnadas a nuevos prospectos con límites seguros según tu plan — sin quemar el número.',
    },
  ];

  readonly personas: Persona[] = [
    { name: 'Restaurantes', example: '“¿Tienen mesa para 6?” → Interesados → tú confirmas y cierras.”' },
    { name: 'Tiendas online', example: '“¿Cuánto cuesta el envío?” → IA responde → comprador listo en tu bandeja.”' },
    { name: 'Servicios', example: '“¿Agenda para el viernes?” → detecta interés → tú cotizas y agendas.”' },
  ];
}
