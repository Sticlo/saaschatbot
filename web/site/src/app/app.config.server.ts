import { mergeApplicationConfig, ApplicationConfig } from '@angular/core';
import { provideServerRendering } from '@angular/platform-server';

import { appConfig } from './app.config';
import { API_BASE_URL } from './core/tokens';

const serverApiBase =
  process.env['API_INTERNAL_URL'] || 'http://127.0.0.1:8000';

const serverConfig: ApplicationConfig = {
  providers: [
    provideServerRendering(),
    { provide: API_BASE_URL, useValue: serverApiBase },
  ],
};

export const config = mergeApplicationConfig(appConfig, serverConfig);
