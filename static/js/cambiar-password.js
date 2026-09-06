document.addEventListener("DOMContentLoaded", async () => {
  const form = document.getElementById("change-password-form");
  const errorBox = document.getElementById("change-password-error");
  const mismatchBox = document.getElementById("password-mismatch-error");
  const submitButton = form.querySelector("button[type=submit]");

  // Página protegida: si no hay access token en memoria (recarga de
  // página, o se llegó directo a esta URL), se intenta recuperar la
  // sesión vía /auth/refresh antes de dejar usar el formulario. Si
  // tampoco eso funciona, requireSession ya redirige a /login.
  const token = await Auth.requireSession();
  if (!token) {
    return;
  }
  form.hidden = false;

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    errorBox.hidden = true;
    mismatchBox.hidden = true;

    const passwordActual = form.password_actual.value;
    const passwordNueva = form.password_nueva.value;
    const passwordNuevaConfirmar = form.password_nueva_confirmar.value;

    if (passwordNueva !== passwordNuevaConfirmar) {
      mismatchBox.hidden = false;
      return;
    }

    submitButton.disabled = true;
    try {
      const response = await Auth.apiFetch("/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          password_actual: passwordActual,
          password_nueva: passwordNueva,
        }),
      });

      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        errorBox.textContent = extractErrorMessage(data);
        errorBox.hidden = false;
        return;
      }

      // El backend invalida la sesión de refresh al cambiar la clave:
      // hay que loguearse de nuevo con la clave nueva.
      Auth.clearAccessToken();
      window.location.href = "/login";
    } catch (error) {
      errorBox.textContent = "No se pudo conectar con el servidor. Intenta de nuevo.";
      errorBox.hidden = false;
    } finally {
      submitButton.disabled = false;
    }
  });

  function extractErrorMessage(data) {
    if (typeof data.detail === "string") {
      return data.detail;
    }
    if (Array.isArray(data.detail) && data.detail.length > 0) {
      return data.detail[0].msg || "La contraseña nueva no es válida.";
    }
    return "No se pudo cambiar la contraseña. Intenta de nuevo.";
  }
});
