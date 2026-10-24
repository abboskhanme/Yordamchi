"""Owner admission (SPEC §3.2.1 step 2). T14 adds the `admit` policy; this module starts with `OwnerOrigin`.

`OwnerOrigin` is the proof that an action comes from a verified owner update. A settings change, an approval and a
decision binding all require one, so executor output, tool results and web content can never change a setting or
approve a decision (CONVENTIONS §5, A5: `OwnerOrigin(` is called only inside `shared/access/`).
"""

from dataclasses import dataclass

from yordamchi.core.paths import BotName


@dataclass(frozen=True, slots=True)
class OwnerOrigin:
    bot: BotName
    update_id: int
    user_id: int
    chat_id: int
