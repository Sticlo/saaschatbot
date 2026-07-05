import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, catchError, of, timeout } from 'rxjs';

import { API_BASE_URL } from '../tokens';
import { Plan } from '../models/plan.model';

const API_TIMEOUT_MS = 8000;

@Injectable({ providedIn: 'root' })
export class PlansService {
  private readonly http = inject(HttpClient);
  private readonly apiBase = inject(API_BASE_URL);

  listPublicPlans(): Observable<Plan[]> {
    return this.http.get<Plan[]>(`${this.apiBase}/api/v1/plans`).pipe(
      timeout(API_TIMEOUT_MS),
      catchError(() => of([])),
    );
  }
}
