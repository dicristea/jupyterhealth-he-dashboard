// Speech "not connected yet" dialog: open from any [data-speech-open]; Esc, Close or the backdrop closes it.
(function () {
    const modal = document.getElementById('speechModal');
    if (!modal) return;
    let opener = null;
    const focusables = () => modal.querySelectorAll('a[href], button');
    function open(e) {
        e.preventDefault();  // links fall back to the code page when JS is off
        opener = document.activeElement;
        modal.hidden = false;
        focusables()[0].focus();
    }
    function close() {
        modal.hidden = true;
        if (opener) opener.focus();
    }
    document.querySelectorAll('[data-speech-open]').forEach(el => el.addEventListener('click', open));
    modal.querySelectorAll('[data-speech-close]').forEach(el => el.addEventListener('click', close));
    modal.addEventListener('click', e => { if (e.target === modal) close(); });
    modal.addEventListener('keydown', e => {
        if (e.key === 'Escape') { close(); return; }
        if (e.key !== 'Tab') return;
        const f = focusables(), first = f[0], last = f[f.length - 1];  // keep Tab inside the dialog
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
})();
