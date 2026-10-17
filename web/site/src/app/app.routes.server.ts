import { RenderMode, ServerRoute } from '@angular/ssr';

/** Todo se genera como HTML en el build: el visitante ve la página antes de que cargue el JS. */
export const serverRoutes: ServerRoute[] = [{ path: '**', renderMode: RenderMode.Prerender }];
