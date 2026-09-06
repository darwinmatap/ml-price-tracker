/**
 * Gestión de sesión del lado del cliente.
 *
 * El access token vive SOLO en una variable en memoria (nunca en
 * localStorage/sessionStorage): reduce la superficie de exposición si
 * algún día hay un XSS, a costa de perderse al recargar la página a
 * propósito. Para eso existe ensureAccessToken()/requireSession(): usan
 * POST /auth/refresh (la cookie HttpOnly del refresh token viaja sola,
 * el JS nunca la toca) para recuperar un access token nuevo sin pedir
 * login de nuevo.
 *
 * Se comparte entre todas las páginas protegidas vía <script src="/static/js/auth.js">.
 */
const Auth = (() => {
  let accessToken = null;

  function setAccessToken(token) {
    accessToken = token;
  }

  function getAccessToken() {
    return accessToken;
  }

  function clearAccessToken() {
    accessToken = null;
  }

  /** Intenta obtener un access token nuevo vía la cookie de refresh. */
  async function refresh() {
    let response;
    try {
      response = await fetch("/auth/refresh", {
        method: "POST",
        credentials: "same-origin",
      });
    } catch (error) {
      clearAccessToken();
      return null;
    }

    if (!response.ok) {
      clearAccessToken();
      return null;
    }

    const data = await response.json();
    setAccessToken(data.access_token);
    return accessToken;
  }

  /** Devuelve el access token actual, o intenta recuperarlo vía refresh si no hay uno en memoria. */
  async function ensureAccessToken() {
    if (accessToken) {
      return accessToken;
    }
    return refresh();
  }

  /**
   * Para páginas protegidas: exige una sesión válida antes de dejar
   * usar la página. Si no se puede recuperar ninguna (ni en memoria ni
   * vía refresh), redirige a /login.
   */
  async function requireSession({ redirectTo = "/login" } = {}) {
    const token = await ensureAccessToken();
    if (!token) {
      window.location.replace(redirectTo);
      return null;
    }
    return token;
  }

  /** fetch() que agrega el Authorization: Bearer automáticamente si hay token en memoria. */
  async function apiFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (accessToken) {
      headers.set("Authorization", `Bearer ${accessToken}`);
    }
    return fetch(url, { ...options, headers, credentials: "same-origin" });
  }

  return {
    setAccessToken,
    getAccessToken,
    clearAccessToken,
    refresh,
    ensureAccessToken,
    requireSession,
    apiFetch,
  };
})();
