import { AsyncPipe } from '@angular/common';
import { Component, inject } from '@angular/core';
import { RouterLink } from '@angular/router';

import { CONTACT, whatsappUrl } from '../../core/contact';
import { LEGAL } from '../../core/legal';
import { appUrl } from '../../core/oauth-url';
import { SessionService } from '../../core/services/session.service';
import { environment } from '../../../environments/environment';
import { ShellComponent } from '../../layout/shell/shell.component';

/** Para mostrar foto, súbela a public/images/ y pon aquí su ruta (ej. '/images/fundador.jpg'). */
const FOUNDER = {
  name: 'Juan Aguilar',
  role: 'Fundador de Omitel',
  photo: '',
  quote:
    'Un negocio pequeño no debería perder una venta por contestar tarde. Construí Omitel para que la IA ' +
    'atienda lo repetitivo y el dueño solo entre cuando alguien de verdad quiere comprar.',
};

interface Principle {
  title: string;
  text: string;
}

@Component({
  selector: 'app-about',
  imports: [ShellComponent, RouterLink, AsyncPipe],
  templateUrl: './about.component.html',
  styleUrl: './company.scss',
})
export class AboutComponent {
  private readonly session = inject(SessionService);

  readonly user$ = this.session.user$;
  readonly founder = FOUNDER;
  readonly founderInitials = FOUNDER.name
    .split(' ')
    .map((part) => part[0])
    .slice(0, 2)
    .join('');
  readonly legal = LEGAL;
  readonly contact = CONTACT;
  readonly whatsappHref = whatsappUrl('Hola, quiero saber más de Omitel.');

  readonly principles: Principle[] = [
    {
      title: 'Hacemos una sola cosa',
      text: 'Omitel es un asistente de ventas para WhatsApp, y nada más. Todo nuestro tiempo va a que funcione cada vez mejor para tu negocio.',
    },
    {
      title: 'Tú tienes el control',
      text: 'La IA responde lo repetitivo, pero tú decides en qué chats actúa. Con un clic tomas cualquier conversación y la IA se pausa solo ahí.',
    },
    {
      title: 'Precio claro, sin amarres',
      text: 'Un solo plan con todo incluido, sin contratos ni cobros escondidos. Cancelas cuando quieras desde Mi plan, sin llamar a nadie.',
    },
    {
      title: 'Tus datos son tuyos',
      text: 'No vendemos tu información ni la de tus clientes. Si cancelas tu cuenta, borramos tus datos en 30 días.',
    },
  ];

  get panelUrl(): string {
    return appUrl(environment.panelUrl);
  }
}
