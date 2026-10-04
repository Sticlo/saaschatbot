import { Component } from '@angular/core';
import { RouterLink } from '@angular/router';

import { LEGAL } from '../../../core/legal';
import { ShellComponent } from '../../../layout/shell/shell.component';

@Component({
  selector: 'app-privacy',
  imports: [ShellComponent, RouterLink],
  templateUrl: './privacy.component.html',
  styleUrl: '../legal.scss',
})
export class PrivacyComponent {
  readonly legal = LEGAL;
}
