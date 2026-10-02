/* ============================================================================ */
/* modal.js                                                                     */
/* Promise-based alert and confirm helpers for the two shared modals.          */
/* ============================================================================ */

/* ---------------------------------------------------------------------------- */
/* Alert — informational message with OK button                                 */
/* ---------------------------------------------------------------------------- */

export function showAlert(title, message) {
  return new Promise((resolve) => {
    document.getElementById("alert-modal-title").textContent   = title;
    document.getElementById("alert-modal-message").textContent = message;

    const modal = document.getElementById("alert-modal");
    const btn   = document.getElementById("alert-modal-ok");

    modal.classList.remove("hidden");

    function dismiss() {
      modal.classList.add("hidden");
      btn.removeEventListener("click", dismiss);
      resolve();
    }

    btn.addEventListener("click", dismiss);
  });
}

/* ---------------------------------------------------------------------------- */
/* Confirm — returns true if user clicks Confirm, false on Cancel              */
/* ---------------------------------------------------------------------------- */

export function showConfirm(title, message) {
  return new Promise((resolve) => {
    document.getElementById("confirm-modal-title").textContent   = title;
    document.getElementById("confirm-modal-message").textContent = message;

    const modal     = document.getElementById("confirm-modal");
    const btnOk     = document.getElementById("confirm-modal-ok");
    const btnCancel = document.getElementById("confirm-modal-cancel");

    modal.classList.remove("hidden");

    function dismiss(result) {
      modal.classList.add("hidden");
      btnOk.removeEventListener("click", onOk);
      btnCancel.removeEventListener("click", onCancel);
      resolve(result);
    }

    const onOk     = () => dismiss(true);
    const onCancel = () => dismiss(false);

    btnOk.addEventListener("click", onOk);
    btnCancel.addEventListener("click", onCancel);
  });
}
