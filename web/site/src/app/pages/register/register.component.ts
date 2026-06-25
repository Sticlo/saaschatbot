import { NgIf } from '@angular/common';
import { Component, inject } from '@angular/core';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { RouterLink } from '@angular/router';

import { ShellComponent } from '../../layout/shell/shell.component';
import { AuthService } from '../../core/services/auth.service';

@Component({
  selector: 'app-register',
  imports: [ShellComponent, ReactiveFormsModule, RouterLink, NgIf],
  templateUrl: './register.component.html',
  styleUrl: './register.component.scss',
})
export class RegisterComponent {
  private readonly fb = inject(FormBuilder);
  private readonly auth = inject(AuthService);

  submitting = false;
  error = '';

  readonly form = this.fb.nonNullable.group({
    business_name: ['', [Validators.required, Validators.minLength(2)]],
    owner_name: ['', [Validators.required, Validators.minLength(2)]],
    email: ['', [Validators.required, Validators.email]],
    password: ['', [Validators.required, Validators.minLength(8)]],
  });

  submit(): void {
    if (this.form.invalid || this.submitting) {
      this.form.markAllAsTouched();
      return;
    }

    this.submitting = true;
    this.error = '';

    this.auth.register(this.form.getRawValue()).subscribe({
      error: (err) => {
        this.submitting = false;
        this.error =
          err?.error?.detail ||
          'No pudimos crear la cuenta. Revisa los datos e intenta de nuevo.';
      },
    });
  }
}
