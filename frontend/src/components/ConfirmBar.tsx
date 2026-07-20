interface Props {
  title: string;
  subtitle?: string;
  onConfirm: () => void;
  onCancel: () => void;
  confirmLabel?: string;
  cancelLabel?: string;
}

export default function ConfirmBar({
  title,
  subtitle,
  onConfirm,
  onCancel,
  confirmLabel = "입력",
  cancelLabel = "취소",
}: Props) {
  return (
    <div className="confirm-bar" role="region" aria-label={title}>
      <div className="confirm-bar__text">
        <strong>{title}</strong>
        {subtitle ? <span>{subtitle}</span> : null}
      </div>
      <div className="confirm-bar__actions">
        <button type="button" className="btn btn--ghost" onClick={onCancel}>
          {cancelLabel}
        </button>
        <button type="button" className="btn btn--primary" onClick={onConfirm}>
          {confirmLabel}
        </button>
      </div>
    </div>
  );
}
