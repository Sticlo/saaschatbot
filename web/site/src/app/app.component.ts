import { Component } from '@angular/core';
import { RouterOutlet } from '@angular/router';

import { CookieBannerComponent } from './layout/cookie-banner/cookie-banner.component';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, CookieBannerComponent],
  template: '<router-outlet /><app-cookie-banner />',
  styles: ':host { display: block; min-height: 100vh; }',
})
export class AppComponent {}
