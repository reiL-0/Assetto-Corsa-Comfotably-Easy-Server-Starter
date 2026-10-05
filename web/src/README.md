# web/src

| Archivo | Qué es |
|---|---|
| `main.tsx` | Arranque: monta `<App/>` con `MantineProvider` (tema oscuro), `QueryClientProvider` y `BrowserRouter`. |
| `App.tsx` | Única pantalla: cabecera y consulta de `GET /healthz` (`useQuery`). |
| `vite-env.d.ts` | Tipos de Vite. |

Al añadir pantallas, usar solo endpoints de `/api/v1` (la UI no tiene rutas privilegiadas).
