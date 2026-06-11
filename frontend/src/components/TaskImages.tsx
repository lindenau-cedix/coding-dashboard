import { useEffect, useState } from "react";
import { fetchTaskImage } from "../api";

/**
 * Thumbnails of a task's image attachments. Images are fetched with auth
 * headers into object URLs (a plain <img src> cannot send the Bearer token).
 */
export default function TaskImages({
  taskId,
  names,
}: {
  taskId: string;
  names: string[];
}) {
  const [urls, setUrls] = useState<Record<string, string>>({});

  useEffect(() => {
    let active = true;
    const created: string[] = [];
    setUrls({});
    (async () => {
      for (const name of names) {
        try {
          const url = await fetchTaskImage(taskId, name);
          created.push(url);
          if (!active) return;
          setUrls((u) => ({ ...u, [name]: url }));
        } catch {
          /* missing image -> filename placeholder stays */
        }
      }
    })();
    return () => {
      active = false;
      created.forEach((u) => URL.revokeObjectURL(u));
    };
  }, [taskId, names]);

  if (!names.length) return null;
  return (
    <div className="flex flex-wrap gap-2">
      {names.map((name) =>
        urls[name] ? (
          <a key={name} href={urls[name]} target="_blank" rel="noreferrer" title={name}>
            <img
              src={urls[name]}
              alt={name}
              className="h-20 w-20 rounded-lg border border-slate-700 object-cover transition hover:border-cyan-500"
            />
          </a>
        ) : (
          <div
            key={name}
            title={name}
            className="flex h-20 w-20 items-center justify-center overflow-hidden rounded-lg border border-slate-800 bg-slate-950 px-1 text-center text-[10px] text-slate-500"
          >
            {name}
          </div>
        ),
      )}
    </div>
  );
}
