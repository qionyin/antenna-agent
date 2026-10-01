import React from "react";

export function Metric({ label: name, value, suffix, icon: Icon }) {
  return (
    <div className="metric">
      <span>{Icon ? <Icon size={15} /> : null}{name}</span>
      <strong>{value}{suffix && value !== "not_available" ? ` ${suffix}` : ""}</strong>
    </div>
  );
}

export function List({ items, render }) {
  if (!items.length) return <p className="empty">暂无</p>;
  return (
    <ul className="denseList">
      {items.map((item, index) => <li key={item.id || item.path || item.markdown_path || index}>{render(item)}</li>)}
    </ul>
  );
}
