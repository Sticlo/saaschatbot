import 'zone.js';

import { bootstrapApplication } from '@angular/platform-browser';
import { appConfig } from './app/app.config';
import { AppComponent } from './app/app.component';

bootstrapApplication(AppComponent, appConfig).catch((err) => {
  console.error(err);
  const root = document.querySelector('app-root');
  if (root) {
    root.innerHTML =
      '<div style="padding:2rem;font-family:system-ui;text-align:center">' +
      '<p>No pudimos cargar Omitel.</p>' +
      '<p style="color:#6d7080">Recarga la página o abre <a href="http://127.0.0.1:4200">127.0.0.1:4200</a></p>' +
      '</div>';
  }
});
