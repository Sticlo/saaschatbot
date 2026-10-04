import { Component } from '@angular/core';
import { RouterLink } from '@angular/router';

import { LEGAL } from '../../../core/legal';
import { ShellComponent } from '../../../layout/shell/shell.component';

@Component({
  selector: 'app-terms',
  imports: [ShellComponent, RouterLink],
  templateUrl: './terms.component.html',
  styleUrl: '../legal.scss',
})
export class TermsComponent {
  readonly legal = LEGAL;
}
