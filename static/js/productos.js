/**
 * Vista "mis productos". Todo el contenido dinámico (título, precios,
 * mensajes de error) se inserta con textContent o construyendo nodos con
 * createElement — nunca innerHTML — mismo criterio que login/cambiar-password.
 */
document.addEventListener("DOMContentLoaded", async () => {
  const greetingEl = document.getElementById("user-greeting");
  const logoutButton = document.getElementById("logout-button");
  const productsGrid = document.getElementById("products-grid");
  const emptyState = document.getElementById("empty-state");
  const addForm = document.getElementById("add-product-form");
  const addUrlInput = document.getElementById("product-url");
  const addError = document.getElementById("add-product-error");
  const addButton = addForm.querySelector("button[type=submit]");

  // Página protegida: sin sesión válida (ni en memoria ni recuperable vía
  // /auth/refresh), requireSession ya redirige a /login.
  const token = await Auth.requireSession();
  if (!token) {
    return;
  }

  await loadUser();
  await loadProducts();

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

  addForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    addError.hidden = true;
    addButton.disabled = true;
    const originalLabel = addButton.textContent;
    addButton.textContent = "Agregando...";

    try {
      const response = await Auth.apiFetch("/products", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: addUrlInput.value }),
      });
      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        // Mensaje del backend tal cual (409 duplicado, 422 dominio/URL
        // inválida) — sin inventar texto adicional.
        addError.textContent = data.detail || "No se pudo agregar el producto.";
        addError.hidden = false;
        return;
      }

      productsGrid.appendChild(createProductCard(data));
      updateEmptyState();
      addUrlInput.value = "";
    } catch (error) {
      addError.textContent = "No se pudo conectar con el servidor. Intenta de nuevo.";
      addError.hidden = false;
    } finally {
      addButton.disabled = false;
      addButton.textContent = originalLabel;
    }
  });

  async function loadUser() {
    const response = await Auth.apiFetch("/auth/me");
    if (!response.ok) {
      window.location.replace("/login");
      return;
    }
    const me = await response.json();
    greetingEl.textContent = `Hola, ${me.nombre}`;
  }

  async function loadProducts() {
    const response = await Auth.apiFetch("/products");
    if (!response.ok) {
      return;
    }
    const products = await response.json();
    productsGrid.replaceChildren();
    products.forEach((product) => productsGrid.appendChild(createProductCard(product)));
    updateEmptyState();
  }

  function updateEmptyState() {
    const hasProducts = productsGrid.children.length > 0;
    emptyState.hidden = hasProducts;
    productsGrid.hidden = !hasProducts;
  }

  function formatPrice(value) {
    return Number(value).toLocaleString("es-CL", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  function formatDate(isoString) {
    return new Date(isoString).toLocaleString("es-CL");
  }

  /** Construye el indicador de tendencia (▼ verde si bajó, ▲ rojo si subió) o null si no aplica. */
  function buildTrend(actual, anterior) {
    if (actual === null || actual === undefined || anterior === null || anterior === undefined) {
      return null;
    }
    if (actual === anterior) {
      return null;
    }
    const bajo = actual < anterior;
    const trend = document.createElement("span");
    trend.className = `price-trend ${bajo ? "price-trend--down" : "price-trend--up"}`;
    trend.textContent = `${bajo ? "▼" : "▲"} antes ${formatPrice(anterior)}`;
    return trend;
  }

  function createProductCard(product) {
    const card = document.createElement("article");
    card.className = "product-card";
    card.dataset.productId = String(product.id);
    card.dataset.lastPrice = product.precio_actual !== null && product.precio_actual !== undefined
      ? String(product.precio_actual)
      : "";

    const title = document.createElement("h2");
    title.className = "product-card__title";
    title.textContent = product.title || product.item_id;
    card.appendChild(title);

    const priceRow = document.createElement("div");
    priceRow.className = "product-card__price";

    const priceValue = document.createElement("span");
    priceValue.className = "product-card__price-current";
    priceValue.textContent =
      product.precio_actual !== null && product.precio_actual !== undefined
        ? formatPrice(product.precio_actual)
        : "Sin datos de precio";
    priceRow.appendChild(priceValue);

    if (product.moneda) {
      const currency = document.createElement("span");
      currency.className = "product-card__price-currency";
      currency.textContent = product.moneda;
      priceRow.appendChild(currency);
    }
    card.appendChild(priceRow);

    const trend = buildTrend(product.precio_actual, product.precio_anterior);
    if (trend) {
      card.appendChild(trend);
    }

    const meta = document.createElement("p");
    meta.className = "product-card__meta";
    meta.textContent = product.fecha_ultima_revision
      ? `Última revisión: ${formatDate(product.fecha_ultima_revision)}`
      : "Todavía sin escanear";
    card.appendChild(meta);

    const cardError = document.createElement("div");
    cardError.className = "message message--error";
    cardError.hidden = true;
    card.appendChild(cardError);

    const actions = document.createElement("div");
    actions.className = "product-card__actions";

    const scanButton = document.createElement("button");
    scanButton.type = "button";
    scanButton.className = "btn btn-secondary";
    scanButton.textContent = "Escanear ahora";
    scanButton.addEventListener("click", () => scanProduct(product.id, card, scanButton, cardError));
    actions.appendChild(scanButton);

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "btn btn-danger";
    deleteButton.textContent = "Eliminar";
    deleteButton.addEventListener("click", () => deleteProduct(product.id, card, deleteButton));
    actions.appendChild(deleteButton);

    card.appendChild(actions);

    return card;
  }

  async function scanProduct(productId, card, button, errorBox) {
    errorBox.hidden = true;
    button.disabled = true;
    const originalLabel = button.textContent;
    button.textContent = "Escaneando...";

    try {
      const response = await Auth.apiFetch(`/products/${productId}/scan`, { method: "POST" });
      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        errorBox.textContent = data.detail || "No se pudo escanear el producto.";
        errorBox.hidden = false;
        return;
      }

      // La respuesta de /scan trae price/currency/checked_at (nombres
      // distintos a los del listado: precio_actual/moneda).
      updateCardAfterScan(card, data);
    } catch (error) {
      errorBox.textContent = "No se pudo conectar con el servidor. Intenta de nuevo.";
      errorBox.hidden = false;
    } finally {
      button.disabled = false;
      button.textContent = originalLabel;
    }
  }

  function updateCardAfterScan(card, priceCheck) {
    const previousPrice = card.dataset.lastPrice ? Number(card.dataset.lastPrice) : null;
    const newPrice = Number(priceCheck.price);

    const priceValueEl = card.querySelector(".product-card__price-current");
    priceValueEl.textContent = formatPrice(newPrice);

    let currencyEl = card.querySelector(".product-card__price-currency");
    if (!currencyEl) {
      currencyEl = document.createElement("span");
      currencyEl.className = "product-card__price-currency";
      priceValueEl.after(currencyEl);
    }
    currencyEl.textContent = priceCheck.currency;

    const existingTrend = card.querySelector(".price-trend");
    if (existingTrend) {
      existingTrend.remove();
    }
    const trend = buildTrend(newPrice, previousPrice);
    if (trend) {
      card.querySelector(".product-card__price").after(trend);
    }

    const meta = card.querySelector(".product-card__meta");
    meta.textContent = `Última revisión: ${formatDate(priceCheck.checked_at)}`;

    card.dataset.lastPrice = String(newPrice);
  }

  async function deleteProduct(productId, card, button) {
    const confirmed = window.confirm("¿Eliminar este producto? Se perderá su historial de precios.");
    if (!confirmed) {
      return;
    }

    button.disabled = true;
    try {
      const response = await Auth.apiFetch(`/products/${productId}`, { method: "DELETE" });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        window.alert(data.detail || "No se pudo eliminar el producto.");
        button.disabled = false;
        return;
      }
      card.remove();
      updateEmptyState();
    } catch (error) {
      window.alert("No se pudo conectar con el servidor. Intenta de nuevo.");
      button.disabled = false;
    }
  }
});
