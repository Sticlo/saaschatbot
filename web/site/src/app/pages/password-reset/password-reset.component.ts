import { Component, OnInit, inject } from '@angular/core';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { NgIf } from '@angular/common';

import { ShellComponent } from '../../layout/shell/shell.component';
import { AuthService } from '../../core/services/auth.service';

@Component({
  selector: 'app-password-reset',
  imports: [ShellComponent, ReactiveFormsModule, RouterLink, NgIf],
  templateUrl: './password-reset.component.html',
  styleUrl: './password-reset.component.scss',
})
export class PasswordResetComponent implements OnInit {
  private readonly fb = inject(FormBuilder);
  private readonly auth = inject(AuthService);
  private readonly route = inject(ActivatedRoute);

  mode: 'forgot' | 'reset' = 'forgot';
  token = '';
  submitting = false;
  error = '';
  success = '';
  devLink = '';

  readonly emailForm = this.fb.nonNullable.group({
    email: ['', [Validators.required, Validators.email]],
  });

  readonly passwordForm = this.fb.nonNullable.group({
    password: ['', [Validators.required, Validators.minLength(8)]],
    confirm: ['', [Validators.required, Validators.minLength(8)]],
  });

  ngOnInit(): void {
    this.token = this.route.snapshot.queryParamMap.get('token')?.trim() || '';
    this.mode = this.token ? 'reset' : 'forgot';
  }

  submitForgot(): void {
    if (this.emailForm.invalid || this.submitting) {
      this.emailForm.markAllAsTouched();
      return;
    }
    this.submitting = true;
    this.error = '';
    this.success = '';
    this.devLink = '';

    const email = this.emailForm.controls.email.value.trim().toLowerCase();
    this.auth.forgotPassword({ email }).subscribe({
      next: (res) => {
        this.submitting = false;
        this.success = res.message;
        this.devLink = res.dev_link || '';
      },
      error: (err) => {
        this.submitting = false;
        this.error = err?.error?.detail || 'No pudimos procesar la solicitud.';
      },
    });
  }

  submitReset(): void {
    if (this.passwordForm.invalid || this.submitting) {
      this.passwordForm.markAllAsTouched();
      return;
    }

    const password = this.passwordForm.controls.password.value;
    const confirm = this.passwordForm.controls.confirm.value;
    if (password !== confirm) {
      this.error = 'Las contraseñas no coinciden.';
      return;
    }

    this.submitting = true;
    this.error = '';

    this.auth.resetPassword({ token: this.token, password }).subscribe({
      error: (err) => {
        this.submitting = false;
        this.error = err?.error?.detail || 'No pudimos restablecer la contraseña.';
      },
    });
  }
}
