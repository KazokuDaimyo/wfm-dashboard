// Lecture d'un build sur une page Overframe, puis envoi au tableau de bord.
// Source unique, servie par server.py au bookmarklet et au script Tampermonkey :
// s'exécute dans l'onglet Overframe et ne doit donc rien référencer d'extérieur.
function overframeExport(DASH) {
  const nd = document.getElementById("__NEXT_DATA__");
  let pp = null;
  try { pp = JSON.parse(nd.textContent).props.pageProps; } catch (e) {}
  const urlId = (location.pathname.match(/^\/build\/(\d+)\//) || [])[1];
  if (location.hostname !== "overframe.gg" || !urlId || !pp || !pp.buildState) {
    alert("Ouvre d'abord la page d'un build sur Overframe.");
    return;
  }
  if (pp.data && String(pp.data.id) !== urlId) {
    alert("Les données de la page ne correspondent pas à ce build : recharge la page (F5) puis réessaie.");
    return;
  }
  const rarity = el => {
    const card = el.closest('[class*="Mod_mod__"], [class*="ArcaneMod_arcaneMod__"]');
    const m = ((card && card.className) || "").match(/_(common|uncommon|rare|legendary|riven|amalgam|galvanized|archon)__/i);
    return m ? m[1].toLowerCase() : null;
  };
  // Données internes de la carte (React) : numéro de slot et capacité, absents du texte affiché.
  const cardProps = el => {
    const card = el.closest('[class*="Mod_container__"]');
    const key = card && Object.keys(card).find(k => k.startsWith("__reactFiber"));
    for (let f = key && card[key], i = 0; f && i < 6; f = f.return, i++) {
      const p = f.memoizedProps;
      if (p && typeof p === "object" && typeof p.name === "string" && "slot" in p) return p;
    }
    return null;
  };
  // Polarités : icônes de la police d'Overframe, dont la classe porte le code du jeu (AP_ATTACK…).
  const apCode = el => {
    const m = el && typeof el.className === "string" && el.className.match(/wfic-(AP_[A-Z_]+)/);
    return m ? m[1] : null;
  };
  const polarity = el => {
    const card = el.closest('[class*="Mod_container__"]');
    if (!card) return {};
    const slotIcon = card.parentElement.querySelector(':scope > [class*="ModSlot_slotPolarity__"]');
    const drainEl = card.querySelector('[class*="Mod_drain__"]');
    const slotPolarity = apCode(slotIcon);
    const modPolarity = apCode(drainEl && drainEl.querySelector("i"));
    const classes = ((slotIcon && slotIcon.className) || "") + " " + ((drainEl && drainEl.className) || "");
    let match = "neutral";
    if (slotPolarity) {
      if (/mismatch/i.test(classes)) match = "mismatch";
      else if (/polarityMatch/.test(classes)) match = "match";
      else match = slotPolarity === modPolarity || slotPolarity === "AP_ANY" ? "match" : "mismatch";
    }
    return {
      slot_polarity: slotPolarity,
      mod_polarity: modPolarity,
      match,
      drain_text: drainEl ? drainEl.textContent.trim() : null,
    };
  };
  const pick = (selector, keep) => [...document.querySelectorAll(selector)]
    .filter(keep || (() => true))
    .map((el, i) => {
      const p = cardProps(el);
      return {
        name: el.textContent.trim(),
        rarity: rarity(el),
        slot: p && Number.isInteger(p.slot) ? p.slot : i + 1,
        // rôle de l'emplacement : 0 ordinaire, 1 aura, 2 posture, 3 exilus
        slot_type: p && Number.isInteger(p.type) ? p.type : null,
        drain: p && typeof p.drain === "number" ? p.drain : null,
        forma: !!(p && p.hasForma),
        ...polarity(el),
      };
    })
    .filter(x => x.name);
  // Type d'objet d'après les catégories Overframe (["warframe"], ["weapon", "melee", …], ["pet", …]).
  const categories = (pp.item && pp.item.categories) || [];
  const kind = ["warframe", "melee", "primary", "secondary"].find(k => categories.includes(k))
    || (categories.includes("pet") ? "companion" : null);
  const slotsBox = document.querySelector('[class*="BuildCalculator_modSlots__"]');
  const data = {
    item: (pp.item && pp.item.name) || "",
    kind,
    // compagnons : 10 emplacements ordinaires, sans aura ni exilus
    ten_slots: !!(slotsBox && /BuildCalculator_tenSlots__/.test(slotsBox.className)),
    title: (pp.data && pp.data.title) || document.title,
    url: location.href.split("#")[0],
    formas: pp.data && Number.isInteger(pp.data.formas) ? pp.data.formas : null,
    mods: pick('[class*="Mod_name__"]', el => !el.closest('[class*="ArcaneMod_"]')),
    arcanes: pick('[class*="ArcaneMod_name__"]'),
  };
  if (!data.mods.length && !data.arcanes.length) {
    alert("Aucun mod trouvé sur cette page : Overframe a peut-être changé sa mise en page.");
    return;
  }
  window.open(DASH + "/#builds&import=" + encodeURIComponent(JSON.stringify(data)), "wfm-dashboard");
}
