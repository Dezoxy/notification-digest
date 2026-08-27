// ── PWA icons (PLAN.md §11.7) ───────────────────────────────────────────
//
// Three PNGs, base64-embedded as source constants. They are NOT files on
// disk: this Worker deploys as one self-contained script with no build
// configuration (see worker.js's file header), so a binary asset either
// lives inline or needs a bundler feature this repo has deliberately never
// adopted. ~8KB of base64 total, against a stylesheet already an order of
// magnitude bigger — the tradeoff is not close.
//
// All three are the SAME geometry as FAVICON_SVG (src/chrome.js), rendered
// on its 64-unit grid, so the tab icon, the Home Screen icon and the
// notification icon are visibly one mark:
//
//   rect 0,0,64,64 rx=14            fill #4f46e5
//   bar 14,18 36x6 rx=3  white 1.00
//   bar 14,30 28x6 rx=3  white 0.85
//   bar 14,42 20x6 rx=3  white 0.70
//
// What differs between them is framing, and each difference is a platform
// requirement, not a style choice:
//
//   ICON_512          rounded corners, transparent outside them. The
//                     `purpose: "any"` icon — shown as-authored, so it
//                     carries its own corner radius.
//   ICON_MASKABLE_512 full-bleed indigo, art inset 10% on every side. The
//                     `purpose: "maskable"` icon: Android crops it to a
//                     platform mask whose safe zone is the centered 80%,
//                     and un-inset art loses its outermost bar pixels to
//                     that crop.
//   APPLE_TOUCH_ICON  180x180, full-bleed, NO corner radius — iOS applies
//                     its own mask and squircle, and rounding it here would
//                     round it twice.
//
// Quantized to a 32-color palette (the art is four flat colors plus
// antialiasing), which is a 4.5x size cut with no visible banding at any
// size these are displayed at. Regenerating: rasterize the geometry above
// at 4x and downsample with a Lanczos filter. Deliberately no generator
// script in this subtree — it is a Node/prettier subtree (see the repo
// CLAUDE.md), and a one-off Python rasterizer does not get to live here.

export const ICON_512_B64 =
  "iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAMAAADDpiTIAAAAYFBMVEVdWeydmPDX1PlPReZPRuZnYOg+PrtVVaqDfOxaKNz/" +
  "///Y1fgzZv9/f38AAH8Af38zM8zCvvRPRuX+/v7+/v4AAAD+/v5ZUebu7vxFO+UAAP9PRuZVVf9PRuZOReU/P/9VmsoSAAAA" +
  "IHRSTlMV7udwtfYEA+cIAb8FAgICBcf+/9kAsf23/wHQA45MBJcvahcAAA0/SURBVHja7Z3rcuK6EkabmwnMdXMQBMXg93/L" +
  "Y2OSIYSAAWNL+tb6MbOrpiY1273cF1mWzT0Fnxd5/V9mthttt28lC2hIdbW229GuvHj1VSwvp39OpOwZwc/qf3QV+TFhf0yF" +
  "ceVBfTNlz5CgbQGs2P/EMvaEvk0NSguOLm+gAvhpVgd/S+yfYcG2liCb+iAFsGL/62hMqJ7HeLQPWIt5oKWf5LPSyqlx63eR" +
  "CGx6uODBCGDZ/t4n+l05sM8DmQUiQFWSyPx91AI/7V8AX00otiUi3bOtYvfwZGgPhz/fcfP3lQZ2+cMK2IPhp/L33w08pMAD" +
  "AhD+cBToQYDpzM0IfxgKlKGYdixAlf13hD8UBXb31wG7N/vT+oXVDt5bB+y+v8PgF+JQaN0IkLlsxAUPj1EZmA4EKLsNo/iH" +
  "2QrYPjzPFaC0jOwfbh24PQncJsBv4/YPPQnY7+cJUPaZVP/QO4Ebx4FbBCicMfuFPxGaK54igDe34/LGwM6Zb1+AMq/Q/cXS" +
  "C95QBqxx+p+R/uMpA7PGZcAal3+6/7imgaJNAQrKf3yNQNGaAD5n+otxHmz0gNAatf+0f1G2gk2GAWty/xP/SA1okAOuCuAZ" +
  "/2IeB/2jApibEf94DZg1CPCV+5/xL/Jx0D8igDfiH7sBVzpBu9L/sfwXOeMrnaAx/2lPgxcE8AXxT8OAwt8lAPFPx4B7BChY" +
  "/02F0QUDjOc/Alx4MmTf7v8wrls62Lc7RIwFAO3lAPsuAbAAkNpywC0CMADIjAJ2/v6nAUywEcybCuA9DWCKjaD3DQXIPQ1A" +
  "im2Az5sJkLEClCajc2+O2pn3vykAya4GTK8L4POfrACkuhrw8+ujYftaAJgAE54Fs2sCGAUg7SJglwXweUEBSLkIfPn2kJ0u" +
  "ATEBJD4J5JcE8HMKQOpFYO4vCMAzoOQ5fSpkn5cASQDpp4DPC4JGAtBOAcZDQDU+PxY0dgHJjYKfdgcZCUA7BRgJQDsFGAlA" +
  "OwV8COB/kQB0UsAv/0UAEoBmCvgQYM5TIKEUUMxPBSABiKYA+/idRUAhxkeBfz8LiKuixMfZQcarQJJ8vChkvAoimgIOr4kY" +
  "hwGotoHFkQD2hxZQrQ38Y/8EoAXUbQON44BEORwcVGcAHgPo8WYfGYDzgERrQH4QgEUA4aWASoC5pwIo1gA/rwVgBlCeA4wZ" +
  "QHsOKAUwToTRZOyr6FMBtGuAsRVEl2pbiDEEag+C5uYZQ6DqIJjNq8+J0AIoNwFGC6DdBNhfVgGUVwL+WsZ2YOGVAJfZfEYP" +
  "qNsFzuacCyjeBRo9oHYXaPSA2l0gX4eVZss7gepjAOfCaI8BZgwB2mMAAqgLwBSoPQcaU6D2HGhMgdpzIAKoC8AUKD4HIgAC" +
  "AAKArABcA20QAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEA" +
  "AQABAAEAAb5ns9ks1oLs/8flBdgsJoPBcPg/QYbDwWCy2EgLsFkPJGN/ZMFgvZEVgPAHoID1mPwJ/7sCPRaC3gTYTAj/PwUm" +
  "GzUBNgPCfsxgoyUA8Q/FACP+2gYY8dc2oA8BNhOifY5eOsFeMgD9//lZQCQDUABCKgI9CLAm0t+xVhCABBBUCjASgHYKMBKA" +
  "dgroPgMwAgQ1CHQuAGsAl9cCUheAChBYDUAABOhWAFqAy01A6gLQAwbWBXYtAKsAga0EIAACIAACIAAC0ATSBDIGMgayEMRC" +
  "EAIgAA+DeBhEF6jRA7IhRLwCsCVMexWATaHqCYBt4eIJgBdDxBMAr4ZJjwALXg4Nag1A5uVQikAoBYADItTjzxEx4vHnkCjx" +
  "+HNMXBj9v94xcRwUeRR+yYMiFxwV+x5+0aNiUSCA8HNcfJ+x57j4gwN8MEJbAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAA" +
  "QABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAAQABAAEAAToi/1x8dEd" +
  "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";

export const ICON_MASKABLE_512_B64 =
  "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";

export const APPLE_TOUCH_ICON_B64 =
  "iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAMAAAAKE/YAAAAAYFBMVEVPRuX+/v7+/v7+/v5EOuSnovLu7fuemvB2cOtuZ+mP" +
  "iu7W1PhiWue5tvRdVOeBe+ygnPC4tPWUju7CvfbT0PjNy/fKx/bp6fuYku/b2vquqvPh3voAAAAAAAAAAAAAAAAcj1cjAAAA" +
  "IHRSTlP+/9ix//+37fX2+/L4+frs68rax8DwxNzYvc7fAAAAAIsPAWYAAAF1SURBVHja7djdcsIgEEDhBNAEgkls/Wv7/u/Z" +
  "9g007i6uc75rL84wSwC7DgAAAAAAAAAAAAAAAHgtSYticS47FUPulLJTPvdqpqJSnUqv6pD8NWtUp9yrG6Sr06Qf3c/+Frrv" +
  "hTej/kQrTHXaWURPRDsdD48b0eUnz+fh4vIY93lh8nk19fkI8PncAgAAwJvydwdNXS3r8JR1mW2z/55bQcChWmYPQchiVp3E" +
  "mkMYk9VsyDWHq9VKnwWjw2Cz1FWyOfzYTEcRjQ7ZJHqVjTbZij6jF9noajLUs2jzZPSdPkhGF6PTpfpbaNmprp1Z9XgVWme7" +
  "5v/r9CqQPRXzV0Ae8/iEPNauwf/n/OMPAACAl5DUfqwnf3/F/X3i/ri8RPMxPuZU2zef4sNy6+ZL3GBuuwU/tjTHz7YLvd8U" +
  "3XZA8rbmeEvupqPtfKTbxuhLx0q//0z7/Hr4/E57PBGd3j183vJ83qd9vlwAAAAAAAAAAAAAAHDoF7NxLmGOptZLAAAAAElF" +
  "TkSuQmCC";

// Decoded once per isolate, not per request: module top-level runs on cold
// start only, and these bytes never change for the life of a deployment.
function decode(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

export const ICON_512 = decode(ICON_512_B64);

export const ICON_MASKABLE_512 = decode(ICON_MASKABLE_512_B64);

export const APPLE_TOUCH_ICON = decode(APPLE_TOUCH_ICON_B64);
