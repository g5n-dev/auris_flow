

export const initialsForName = (value: string) => {
  const normalized = value.trim();
  if (!normalized) return "U";
  const latin = normalized.match(/[a-zA-Z]/g)?.slice(0, 2).join("");
  return (latin || normalized.slice(0, 2)).toUpperCase();
};
