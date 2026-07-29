/* Shared HTTP contract for every frontend module. */
(function () {
  function detailMessage(detail) {
    if (typeof detail === "string" && detail.trim()) return detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      const message = detail.message || detail.msg;
      if (message) return `${detail.field ? detail.field + ": " : ""}${message}`;
    }
    if (Array.isArray(detail)) {
      const messages = detail.map((item) => {
        if (typeof item === "string") return item;
        if (!item || typeof item !== "object") return "";
        const field = Array.isArray(item.loc) ? item.loc.filter((part) => part !== "body").join(".") : "";
        return `${field ? field + ": " : ""}${item.msg || "Invalid value"}`;
      }).filter(Boolean);
      if (messages.length) return messages.join("; ");
    }
    return "";
  }

  async function apiRequest(url, options) {
    let response;
    try {
      response = await fetch(url, options);
    } catch (error) {
      throw new Error(`Cannot reach the server. ${error.message || "Check that the web service is running."}`);
    }

    const contentType = response.headers.get("content-type") || "";
    const expectsJson = contentType.includes("application/json");
    let payload = null;
    try {
      payload = expectsJson ? await response.json() : await response.text();
    } catch (_error) {
      payload = null;
    }

    if (!response.ok) {
      const detail = expectsJson
        ? detailMessage(payload && (payload.detail || payload.message))
        : (typeof payload === "string" ? payload.trim() : "");
      const error = new Error(detail || `${response.status} ${response.statusText || "Request failed"}`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  window.apiRequest = apiRequest;
  window.apiErrorMessage = detailMessage;
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {apiRequest, detailMessage};
  }
})();
