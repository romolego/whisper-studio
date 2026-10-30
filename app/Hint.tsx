"use client";

export default function Hint({ text }: { text: string }) {
  return (
    <span className="hint">
      <button
        type="button"
        className="hint-mark"
        aria-label={text}
        // Подсказка не должна отправлять формы и не должна перехватывать
        // клик по строке настройки.
        onClick={(event) => event.stopPropagation()}
      >
        ?
      </button>
      <span className="hint-bubble" role="tooltip">
        {text}
      </span>
    </span>
  );
}
