import { APP_INITIALIZER, ApplicationConfig, provideZoneChangeDetection } from '@angular/core';
import { provideRouter } from '@angular/router';
import { provideHttpClient, withInterceptorsFromDi } from '@angular/common/http';

import { routes } from './app.routes';
import { API_BASE_URL } from './core/tokens';
import { SessionService } from './core/services/session.service';
import { environment } from '../environments/environment';

function initSession(session: SessionService): () => Promise<void> {
  return () => {
    session.initialize().subscribe();
    return Promise.resolve();
  };
}

export const appConfig: ApplicationConfig = {
  providers: [
    provideZoneChangeDetection({ eventCoalescing: true }),
    provideRouter(routes),
    provideHttpClient(withInterceptorsFromDi()),
    { provide: API_BASE_URL, useValue: environment.apiUrl },
    {
      provide: APP_INITIALIZER,
      useFactory: initSession,
      deps: [SessionService],
      multi: true,
    },
  ],
};
