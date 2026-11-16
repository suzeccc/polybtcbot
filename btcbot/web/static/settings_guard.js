(() => {
  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('form[data-guard-unsaved]').forEach(form => {
      const snapshot = () => JSON.stringify([...form.elements]
        .filter(element => element.name)
        .map(element => [element.name, element.type === 'checkbox' ? element.checked : element.value]));
      let savedSnapshot = snapshot();
      let allowNavigation = false;
      const isDirty = () => snapshot() !== savedSnapshot;

      form.addEventListener('settings:saved', () => { savedSnapshot = snapshot(); });
      window.addEventListener('beforeunload', event => {
        if (!allowNavigation && isDirty()) {
          event.preventDefault();
          event.returnValue = '';
        }
      });
      document.addEventListener('click', event => {
        const link = event.target.closest('a[href]');
        if (!link || event.defaultPrevented || link.target || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        const target = new URL(link.href, location.href);
        if (target.origin !== location.origin || target.href === location.href || !isDirty()) return;
        if (window.confirm('You have unsaved changes. Leave this page and discard them?')) allowNavigation = true;
        else event.preventDefault();
      });
    });
  });
})();
