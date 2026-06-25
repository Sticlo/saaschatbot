import { Component } from '@angular/core';
import { RouterLink } from '@angular/router';

import { ShellComponent } from '../../layout/shell/shell.component';

@Component({
  selector: 'app-home',
  imports: [ShellComponent, RouterLink],
  templateUrl: './home.component.html',
  styleUrl: './home.component.scss',
})
export class HomeComponent {}
