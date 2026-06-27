import { Component, OnInit, inject } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';

import { ShellComponent } from '../../layout/shell/shell.component';
import { AuthService } from '../../core/services/auth.service';

@Component({
  selector: 'app-magic-verify',
  imports: [ShellComponent, RouterLink],
  templateUrl: './magic-verify.component.html',
  styleUrl: './magic-verify.component.scss',
})
export class MagicVerifyComponent implements OnInit {
  private readonly route = inject(ActivatedRoute);
  private readonly auth = inject(AuthService);

  status: 'loading' | 'error' = 'loading';
  error = '';

  ngOnInit(): void {
    const token = this.route.snapshot.queryParamMap.get('token')?.trim();
    if (!token) {
      this.status = 'error';
      this.error = 'Enlace inválido.';
      return;
    }

    this.auth.verifyMagicLink(token).subscribe({
      error: (err) => {
        this.status = 'error';
        this.error =
          err?.error?.detail ||
          'El enlace expiró o ya fue usado. Pide uno nuevo desde el login.';
      },
    });
  }
}
