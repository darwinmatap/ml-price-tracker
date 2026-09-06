document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("login-form");
  const errorBox = document.getElementById("login-error");
  const successBox = document.getElementById("login-success");
  const submitButton = form.querySelector("button[type=submit]");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorBox.hidden = true;
    successBox.hidden = true;
    submitButton.disabled = true;

    const username = form.username.value;
    const password = form.password.value;

    try {
      const loginResponse = await fetch("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ username, password }),
      });

      const loginData = await loginResponse.json().catch(() => ({}));

      if (!loginResponse.ok) {
        // Se muestra tal cual el mensaje genérico que ya manda el backend
        // (credenciales inválidas, rate limit, bloqueo por intentos) —
        // nunca se agrega detalle adicional del lado del cliente.
        errorBox.textContent = loginData.detail || "Credenciales inválidas";
        errorBox.hidden = false;
        return;
      }

      Auth.setAccessToken(loginData.access_token);

      const meResponse = await Auth.apiFetch("/auth/me");
      if (!meResponse.ok) {
        errorBox.textContent = "No se pudo verificar la sesión. Intenta de nuevo.";
        errorBox.hidden = false;
        Auth.clearAccessToken();
        return;
      }
      const me = await meResponse.json();

      if (me.debe_cambiar_password) {
        window.location.href = "/cambiar-password";
        return;
      }

      // La vista de "mis productos" todavía no existe (siguiente paso).
      // Por ahora se confirma el login acá mismo en vez de redirigir a
      // una ruta que no existe.
      form.hidden = true;
      successBox.textContent = `Sesión iniciada correctamente. Hola, ${me.nombre}.`;
      successBox.hidden = false;
    } catch (error) {
      errorBox.textContent = "No se pudo conectar con el servidor. Intenta de nuevo.";
      errorBox.hidden = false;
    } finally {
      submitButton.disabled = false;
    }
  });
});
