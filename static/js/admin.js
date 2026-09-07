/**
 * Panel de administrador. La protección real vive en el backend
 * (require_admin en app/admin.py) — esta página solo evita mostrarle la
 * UI a alguien sin rol admin, redirigiendo de inmediato a /productos sin
 * ningún mensaje de "no autorizado" (la página simplemente no es para él).
 *
 * Todo el contenido dinámico se inserta con textContent/createElement,
 * nunca innerHTML — mismo criterio que productos.js.
 */
document.addEventListener("DOMContentLoaded", async () => {
  const logoutButton = document.getElementById("logout-button");
  const usersTableBody = document.getElementById("users-table-body");
  const productsTableBody = document.getElementById("products-table-body");
  const createUserForm = document.getElementById("create-user-form");
  const usernameInput = document.getElementById("new-username");
  const nombreInput = document.getElementById("new-nombre");
  const passwordInput = document.getElementById("new-password");
  const createUserError = document.getElementById("create-user-error");
  const createUserButton = createUserForm.querySelector("button[type=submit]");
  const meliConnectButton = document.getElementById("meli-connect-button");
  const meliStatusMessage = document.getElementById("meli-status-message");

  const token = await Auth.requireSession();
  if (!token) {
    return;
  }

  const isAdmin = await verifyAdminOrRedirect();
  if (!isAdmin) {
    return;
  }

  await loadUsers();
  await loadProducts();
  await loadMeliStatus();

  meliConnectButton.addEventListener("click", async () => {
    meliConnectButton.disabled = true;
    try {
      const response = await Auth.apiFetch("/admin/meli/connect");
      const data = await response.json().catch(() => ({}));

      if (!response.ok || !data.authorization_url) {
        window.alert(data.detail || "No se pudo iniciar la conexión con Mercado Libre.");
        meliConnectButton.disabled = false;
        return;
      }

      // Navegación real del navegador (no fetch): recién acá Mercado
      // Libre puede mostrarle al admin su propia pantalla de
      // autorización — el JSON anterior solo trae la URL a la que ir.
      window.location.href = data.authorization_url;
    } catch (error) {
      window.alert("No se pudo conectar con el servidor. Intenta de nuevo.");
      meliConnectButton.disabled = false;
    }
  });

  logoutButton.addEventListener("click", async () => {
    logoutButton.disabled = true;
    try {
      await Auth.apiFetch("/auth/logout", { method: "POST" });
    } catch (error) {
      // Aunque falle la llamada de red, igual se cierra la sesión local.
    }
    Auth.clearAccessToken();
    window.location.href = "/login";
  });

  createUserForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    createUserError.hidden = true;
    createUserButton.disabled = true;
    const originalLabel = createUserButton.textContent;
    createUserButton.textContent = "Creando...";

    try {
      const response = await Auth.apiFetch("/admin/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: usernameInput.value,
          nombre: nombreInput.value,
          password: passwordInput.value,
        }),
      });
      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        // Mensaje del backend tal cual (409 username duplicado, 422
        // validación) — sin inventar texto adicional.
        createUserError.textContent = data.detail || "No se pudo crear el usuario.";
        createUserError.hidden = false;
        return;
      }

      usersTableBody.appendChild(createUserRow(data));
      createUserForm.reset();
    } catch (error) {
      createUserError.textContent = "No se pudo conectar con el servidor. Intenta de nuevo.";
      createUserError.hidden = false;
    } finally {
      createUserButton.disabled = false;
      createUserButton.textContent = originalLabel;
    }
  });

  /** GET /auth/me: si el rol no es admin, redirige a /productos sin mensaje. */
  async function verifyAdminOrRedirect() {
    const response = await Auth.apiFetch("/auth/me");
    if (!response.ok) {
      window.location.replace("/login");
      return false;
    }
    const me = await response.json();
    if (me.role !== "admin") {
      window.location.replace("/productos");
      return false;
    }
    return true;
  }

  async function loadUsers() {
    const response = await Auth.apiFetch("/admin/users");
    if (!response.ok) {
      return;
    }
    const users = await response.json();
    usersTableBody.replaceChildren();
    users.forEach((user) => usersTableBody.appendChild(createUserRow(user)));
  }

  /** Celda con data-label: en mobile, el CSS usa ese atributo para mostrar la etiqueta (patrón de tabla-a-card). */
  function makeCell(label, text) {
    const td = document.createElement("td");
    td.dataset.label = label;
    td.textContent = text;
    return td;
  }

  function createUserRow(user) {
    const row = document.createElement("tr");
    row.dataset.userId = String(user.id);

    row.appendChild(makeCell("Username", user.username));
    row.appendChild(makeCell("Nombre", user.nombre));
    row.appendChild(makeCell("Rol", user.role === "admin" ? "Administrador" : "Usuario"));
    row.appendChild(makeCell("Estado", user.is_active ? "Activo" : "Inactivo"));

    const actionsCell = document.createElement("td");
    actionsCell.dataset.label = "Acciones";

    if (user.is_active) {
      const deactivateButton = document.createElement("button");
      deactivateButton.type = "button";
      deactivateButton.className = "btn btn-danger";
      deactivateButton.textContent = "Desactivar";
      deactivateButton.addEventListener("click", () => deactivateUser(user.id, row, deactivateButton));
      actionsCell.appendChild(deactivateButton);
    }

    row.appendChild(actionsCell);
    return row;
  }

  async function deactivateUser(userId, row, button) {
    const confirmed = window.confirm("¿Desactivar este usuario?");
    if (!confirmed) {
      return;
    }

    button.disabled = true;
    try {
      const response = await Auth.apiFetch(`/admin/users/${userId}/deactivate`, { method: "PATCH" });
      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        // Mensaje específico del backend (ej. "no se puede desactivar al
        // único administrador activo") — nunca uno genérico inventado acá.
        window.alert(data.detail || "No se pudo desactivar el usuario.");
        button.disabled = false;
        return;
      }

      updateRowAfterDeactivate(row, data);
    } catch (error) {
      window.alert("No se pudo conectar con el servidor. Intenta de nuevo.");
      button.disabled = false;
    }
  }

  function updateRowAfterDeactivate(row, user) {
    // Orden fijo de columnas: username, nombre, rol, estado, acciones.
    const cells = row.querySelectorAll("td");
    cells[3].textContent = user.is_active ? "Activo" : "Inactivo";
    cells[4].replaceChildren();
  }

  function formatPrice(value) {
    return Number(value).toLocaleString("es-CL", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  function createProductRow(product) {
    const row = document.createElement("tr");

    row.appendChild(makeCell("Dueño", product.username));
    row.appendChild(makeCell("Título", product.title || product.item_id));
    row.appendChild(
      makeCell(
        "Precio actual",
        product.precio_actual !== null && product.precio_actual !== undefined
          ? `${formatPrice(product.precio_actual)}${product.moneda ? " " + product.moneda : ""}`
          : "Sin datos"
      )
    );
    row.appendChild(
      makeCell(
        "Precio anterior",
        product.precio_anterior !== null && product.precio_anterior !== undefined
          ? formatPrice(product.precio_anterior)
          : "—"
      )
    );

    return row;
  }

  async function loadProducts() {
    const response = await Auth.apiFetch("/admin/products");
    if (!response.ok) {
      return;
    }
    const products = await response.json();
    productsTableBody.replaceChildren();
    products.forEach((product) => productsTableBody.appendChild(createProductRow(product)));
  }

  async function loadMeliStatus() {
    const response = await Auth.apiFetch("/admin/meli/status");
    if (!response.ok) {
      return;
    }
    const status = await response.json();
    renderMeliStatus(status);
  }

  function renderMeliStatus(status) {
    if (!status.connected) {
      meliStatusMessage.hidden = true;
      meliConnectButton.textContent = "Conectar con Mercado Libre";
      return;
    }

    const fecha = status.updated_at ? new Date(status.updated_at).toLocaleString("es-CL") : null;
    meliStatusMessage.textContent = fecha
      ? `Conectado a Mercado Libre (última actualización: ${fecha}).`
      : "Conectado a Mercado Libre.";
    meliStatusMessage.hidden = false;
    meliConnectButton.textContent = "Reconectar con Mercado Libre";
  }
});
