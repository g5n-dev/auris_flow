import { Check, ChevronDown } from "lucide-react";
import { useState } from "react";

export type EntitySelectOption = {
  value: string;
  label: string;
  description?: string;
};

export function EntitySelect({
  value,
  options,
  onChange,
  ariaLabel
}: {
  value: string;
  options: EntitySelectOption[];
  onChange: (value: string) => void;
  ariaLabel: string;
}) {
  const [open, setOpen] = useState(false);
  const selected = options.find((option) => option.value === value) ?? {
    value,
    label: value,
    description: "自定义业务场景草案"
  };

  return (
    <div
      className={open ? "entity-select-field open" : "entity-select-field"}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          setOpen(false);
        }
      }}
      onBlur={(event) => {
        const nextFocus = event.relatedTarget as Node | null;
        if (open && (!nextFocus || !event.currentTarget.contains(nextFocus))) setOpen(false);
      }}
    >
      <button
        type="button"
        className="entity-select-trigger"
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span>
          <b>{selected.label}</b>
          {selected.description && <small>{selected.description}</small>}
        </span>
        <ChevronDown size={16} />
      </button>
      {open && (
        <div className="entity-select-menu" role="listbox" aria-label={ariaLabel}>
          {options.map((option) => (
            <button
              key={option.value}
              type="button"
              className={option.value === value ? "selected" : ""}
              role="option"
              aria-selected={option.value === value}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => {
                onChange(option.value);
                setOpen(false);
              }}
            >
              <span>
                <b>{option.label}</b>
                {option.description && <small>{option.description}</small>}
              </span>
              {option.value === value && <Check size={15} />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
