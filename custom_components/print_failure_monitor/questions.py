"""Question wording for the decision model, and how answers become a per-frame verdict.

Users can override, disable or add failure questions through the `questions` config option, and turn
on the ready-made optional ones. The plate-visible gate can be reworded but not removed.
"""

from __future__ import annotations

DEFAULT_CAMERA_DESCRIPTION = "A camera watching a 3D printer's build plate during a print."


def state_text(camera_description: str, earlier_layers: int | None = None) -> str:
    if earlier_layers is None:
        return f"{camera_description.strip()} The image below is `frame`."
    return (
        f"{camera_description.strip()} The first image below is `earlier`, taken {earlier_layers} printed layers "
        "ago. The second image is `frame`, taken now."
    )


# Gate: frames where the plate can't be seen (dark, blocked) are skipped, not read as "no failure".
VISIBLE = "plate_visible"

BUILTIN = {
    VISIBLE: {
        "instructions": "Is `frame` clear enough to see what is on the build plate?",
        "yes_means": (
            "The image is lit and in focus with the build plate area in view, whether or not anything is on the plate."
        ),
        "no_means": "The image is too dark, blurred, or blocked to see the build plate area.",
    },
    "spaghetti": {
        "instructions": "Is there a tangled mass of loose filament strands on or above the build plate in `frame`?",
        "yes_means": "Curly or tangled strands of filament are piled up or hanging, not forming a solid printed shape.",
        "no_means": (
            "Filament appears only as solid printed parts, support structures, or a few thin strings between parts."
        ),
    },
    # Naming the purge line keeps a near-empty plate early in a print from reading as "maybe detached".
    "detached": {
        "instructions": (
            "Has a printed part broken loose from the build plate or been knocked over in `frame`? The purge line is "
            "the single extruded line or squiggle the printer lays near the edge of the plate before the print "
            "starts. It is not a printed part."
        ),
        "yes_means": (
            "A printed part is tipped over, pushed aside, or stuck to the nozzle instead of sitting on the plate."
        ),
        "no_means": (
            "Printed parts are standing where they were printed, or nothing other than the purge line has been "
            "printed yet."
        ),
    },
    "nozzle_blob": {
        "instructions": "Is a lump of melted filament built up around the print nozzle in `frame`?",
        "yes_means": "A large blob of plastic encases or hangs from the nozzle area.",
        "no_means": "The nozzle area is clean apart from the line currently being printed.",
    },
}


# Off by default: each extra question is another chance of a false alarm, and these are only worth it on
# printers and materials where the failure happens. Turned on with `true` in the questions option.
OPTIONAL = {
    "warping": {
        "instructions": "Has a corner or edge of a printed part lifted or curled up off the build plate in `frame`?",
        "yes_means": "A corner or edge of a printed part is raised above the plate, with a visible gap underneath or a "
        "curled-up edge.",
        "no_means": "The bottom edges of printed parts sit flat on the plate, or nothing has been printed yet.",
    },
}


# Asked only when an earlier frame from the same print is available, a set number of layers back. A part
# that hasn't grown over many layers means the printer is moving without extruding: a clog, a filament
# runout, or printing into air after the part came loose.
NOT_GROWING = "not_growing"
COMPARE = {
    # Tested on pairs from real prints: telling the model to ignore the print head kept it from reading head
    # movement as change, so growing parts stayed clearly below the no threshold.
    NOT_GROWING: {
        "instructions": "Is the printed object in `frame` exactly the same height and size as in `earlier`? Ignore the "
        "print head, which moves between the two images.",
        "yes_means": "The object hasn't grown: it has the same height and outline in both images.",
        "no_means": "The object is taller or wider in `frame`, or there are more printed parts than in `earlier`.",
    },
}


def build_questions(overrides: dict) -> tuple[dict[str, dict], tuple[str, ...]]:
    """Merge user overrides into the built-ins.

    Returns (questions, failure ids). Each question is {"instructions", "yes_means", "no_means"}.
    An override is False (disable a built-in failure question), True (turn on an optional built-in), or a
    dict of fields to replace; a dict for an optional built-in also turns it on, and a new id needs all
    three fields. Raises ValueError on an invalid combination.
    """
    questions = {qid: dict(q) for qid, q in BUILTIN.items()}
    for qid, override in overrides.items():
        if override is True:
            if qid not in OPTIONAL:
                raise ValueError(f"{qid} isn't an optional question; choose from {', '.join(OPTIONAL)}")
            questions[qid] = dict(OPTIONAL[qid])
        elif qid in OPTIONAL and isinstance(override, dict):
            questions[qid] = {**OPTIONAL[qid], **override}
        elif override is False and qid in OPTIONAL:
            continue  # already off
        elif override is False:
            if qid == VISIBLE:
                raise ValueError(f"{VISIBLE} can't be disabled; it stops unreadable frames from counting as clean")
            if qid not in questions:
                raise ValueError(f"can't disable unknown question {qid}")
            del questions[qid]
        elif qid in questions:
            questions[qid].update(override)
        elif set(override) >= {"instructions", "yes_means", "no_means"}:
            questions[qid] = dict(override)
        else:
            raise ValueError(f"new question {qid} needs instructions, yes_means and no_means")
    failures = tuple(qid for qid in questions if qid != VISIBLE)
    if not failures:
        raise ValueError("at least one failure question must stay enabled")
    return questions, failures


def failure_ids(answers: dict[str, float]) -> list[str]:
    """The failure questions a check asked: every answer but the gate."""
    return [q for q in answers if q != VISIBLE]


def judge(answers: dict[str, float], yes: float, no: float) -> tuple[str, bool | None]:
    """(result, verdict) for one check: failed, clean, not_visible or unsure."""
    result_verdict = verdict(answers, yes, no)
    if result_verdict is True:
        return "failed", True
    if result_verdict is False:
        return "clean", False
    return ("not_visible" if answers[VISIBLE] < yes else "unsure"), None


def verdict(answers: dict[str, float], yes: float, no: float) -> bool | None:
    """True = failed frame, False = clean frame, None = not settled (frame is skipped)."""
    if answers[VISIBLE] < yes:
        return None
    probabilities = [answers[q] for q in failure_ids(answers)]
    if any(p > yes for p in probabilities):
        return True
    if all(p < no for p in probabilities):
        return False
    return None
