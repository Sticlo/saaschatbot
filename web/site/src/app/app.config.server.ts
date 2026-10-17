import { mergeApplicationConfig, ApplicationConfig } from '@angular/core';
import { provideServerRendering } from '@angular/platform-server';
import { provideServerRouting } from '@angular/ssr';

import { appConfig } from './app.config';
import { serverRoutes } from './app.routes.server';
import { API_BASE_URL } from './core/tokens';

const serverApiBase =
  process.env['API_INTERNAL_URL'] || 'http://127.0.0.1:8000';

const serverConfig: ApplicationConfig = {
  providers: [
    provideServerRendering(),
    provideServerRouting(serverRoutes),
    { provide: API_BASE_URL, useValue: serverApiBase },
  ],
};

export const config = mergeApplicationConfig(appConfig, serverConfig);
