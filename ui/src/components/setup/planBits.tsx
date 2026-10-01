// Small pieces the Plan step's cards share: the problems under an item, a choice between options,
// an item's Change and Remove buttons, and an editor's frame.

/** What stops this item from being set up, next to it. */
export function Problems({ list, id }: { list?: string[]; id?: string }) {
  if (!list?.length) return null;
  return (
    <ul className="su-problems" id={id}>
      {list.map((m, i) => <li key={i}>{m}</li>)}
    </ul>
  );
}

/** One option in a group of radio choices, laid out as a card to click. */
export function Choice({ name, checked, onChange, title, children, disabled }: {
  name: string; checked: boolean; onChange: () => void; title: React.ReactNode;
  children?: React.ReactNode; disabled?: boolean;
}) {
  return (
    <label className={`su-choice${checked ? " on" : ""}${disabled ? " off" : ""}`}>
      <input type="radio" name={name} checked={checked} disabled={disabled} onChange={onChange} />
      <span>
        <strong>{title}</strong>
        {children && <span className="help">{children}</span>}
      </span>
    </label>
  );
}

/** Change and Remove for one item; `id` is the Change button's, where focus returns. */
export function ItemActions({ id, what, onChange, onRemove, disabled, changeLabel = "Change" }: {
  id: string; what: string; onChange: () => void; onRemove?: () => void; disabled?: boolean;
  changeLabel?: string;
}) {
  return (
    <span className="su-item-actions">
      <button type="button" id={id} className="linklike su-small" disabled={disabled}
              aria-label={`${changeLabel} ${what}`} onClick={onChange}>{changeLabel}</button>
      {onRemove && (
        <button type="button" className="linklike su-small" disabled={disabled}
                aria-label={`Remove ${what}`} onClick={onRemove}>Remove</button>
      )}
    </span>
  );
}

/** An item expanded into its editor: a labelled group with its heading focused on open, Save
 *  and Cancel at the bottom (Save left out when the content saves itself, like the source
 *  form), Escape cancels. Not a <form>: the source form inside is one. */
export function EditorFrame({ title, children, onSave, onCancel, saveLabel = "Save", canSave = true, note }: {
  title: string; children: React.ReactNode; onSave?: () => void; onCancel: () => void;
  saveLabel?: string; canSave?: boolean; note?: React.ReactNode;
}) {
  return (
    <div className="su-editor" role="group" aria-label={title}
         onKeyDown={(e) => {
           // a dropdown inside closes itself on Escape; only an Escape outside one cancels
           if (e.key === "Escape" && !(e.target as HTMLElement).closest(".combo")) { e.stopPropagation(); onCancel(); }
         }}>
      <h3 className="su-h3" tabIndex={-1} ref={(el) => { if (el && !el.dataset.focused) { el.dataset.focused = "1"; el.focus(); } }}>
        {title}
      </h3>
      {children}
      {note}
      <div className="btnrow">
        {onSave && <button type="button" className="primary" disabled={!canSave} onClick={onSave}>{saveLabel}</button>}
        <button type="button" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}
