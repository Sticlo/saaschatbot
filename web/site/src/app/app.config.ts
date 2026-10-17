import { isPlatformBrowser } from '@angular/common';
import { APP_INITIALIZER, ApplicationConfig, PLATFORM_ID, provideZoneChangeDetection } from '@angular/core';
import { provideClientHydration, withEventReplay } from '@angular/platform-browser';
import { provideRouter, withInMemoryScrolling } from '@angular/router';
import { provideHttpClient, withInterceptorsFromDi } from '@angular/common/http';

import { routes } from './app.routes';
import { API_BASE_URL } from './core/tokens';
import { SessionService } from './core/services/session.service';
import { environment } from '../environments/environment';

function initSession(session: SessionService, platformId: object): () => Promise<void> {
  return () => {
    /* En el prerender no hay visitante: la sesión solo se consulta en el navegador. */
    if (isPlatformBrowser(platformId)) {
      session.initialize().subscribe();
    }
    return Promise.resolve();
  };
}

export const appConfig: ApplicationConfig = {
  providers: [
    provideZoneChangeDetection({ eventCoalescing: true }),
    provideRouter(
      routes,
      withInMemoryScrolling({ anchorScrolling: 'enabled', scrollPositionRestoration: 'enabled' }),
    ),
    provideClientHydration(withEventReplay()),
    provideHttpClient(withInterceptorsFromDi()),
    { provide: API_BASE_URL, useValue: environment.apiUrl },
    {
      provide: APP_INITIALIZER,
      useFactory: initSession,
      deps: [SessionService, PLATFORM_ID],
      multi: true,
    },
  ],
};
