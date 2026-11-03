"""Reusable Discord UI views for re-rendering a message in place.

Render callbacks return ``(embed_or_embeds, file_or_None)`` — or, for
PagerView, ``(embed_or_embeds, file_or_None, total_pages)`` — or an error
string, which is shown ephemerally while the message is left untouched.
"""
import math
from typing import Awaitable, Callable, Mapping, Sequence, Union

import discord


def _content_kwargs(embeds, file) -> dict:
    kwargs = {"attachments": [file] if file else []}
    if isinstance(embeds, (list, tuple)):
        kwargs["embeds"] = list(embeds)[:10]
    else:
        kwargs["embed"] = embeds
    return kwargs


class ExpiringView(discord.ui.View):
    """A view whose buttons are greyed out when it times out, instead of
    lingering and failing with "This interaction failed" when clicked.

    Set ``view.message`` to the sent message (followup.send returns it).
    """
    message = None

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def _rerender(self, interaction: discord.Interaction, embeds, file):
        # Keep a handle from *this* interaction — its token is fresh, so the
        # timeout edit still works after the original 15-minute token expires.
        self.message = await interaction.edit_original_response(
            **_content_kwargs(embeds, file), view=self,
        )


class _OptionButton(discord.ui.Button):
    def __init__(self, key: str, label: str, active: bool, row: int):
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary if active else discord.ButtonStyle.secondary,
            row=row,
        )
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        await interaction.response.defer()
        if self.key == view.current:
            return
        result = await view.render(self.key)
        if isinstance(result, str):
            await interaction.followup.send(result, ephemeral=True)
            return
        embeds, file = result
        view.set_current(self.key)
        await view._rerender(interaction, embeds, file)


class OptionView(ExpiringView):
    """A set of buttons (time ranges, box-score sections, …) that each
    re-render the message they're attached to. The active one is highlighted.

    options: a sequence of keys (used as labels) or a {key: label} mapping.
    """
    def __init__(self, options: Union[Sequence[str], Mapping[str, str]], current: str,
                 render: Callable[[str], Awaitable], timeout: float = 600):
        super().__init__(timeout=timeout)
        self.options = dict(options) if isinstance(options, Mapping) else {k: k for k in options}
        self.render = render
        self.set_current(current)

    def set_current(self, current: str):
        self.current = current
        self.clear_items()
        # Spread evenly across the fewest rows (Discord allows 5 per row)
        n = len(self.options)
        per_row = math.ceil(n / math.ceil(n / 5))
        for i, (key, label) in enumerate(self.options.items()):
            self.add_item(_OptionButton(key, label, active=(key == current), row=i // per_row))


class PagerView(ExpiringView):
    """◀ [label] ▶ buttons stepping through pages 1..total (at-bats, innings…).

    render(page) returns (embeds, file, total) so a live game can grow the
    page count between clicks.
    """
    def __init__(self, page: int, total: int, render: Callable[[int], Awaitable],
                 label: Callable[[int, int], str] = lambda p, t: f"{p} / {t}",
                 timeout: float = 600):
        super().__init__(timeout=timeout)
        self.render = render
        self.label_fn = label
        self.prev_btn = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary)
        self.page_btn = discord.ui.Button(style=discord.ButtonStyle.secondary, disabled=True)
        self.next_btn = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary)
        self.prev_btn.callback = lambda interaction: self._go(interaction, -1)
        self.next_btn.callback = lambda interaction: self._go(interaction, +1)
        for b in (self.prev_btn, self.page_btn, self.next_btn):
            self.add_item(b)
        self._set_page(page, total)

    def _set_page(self, page: int, total: int):
        self.page, self.total = page, total
        self.page_btn.label = self.label_fn(page, total)
        self.prev_btn.disabled = page <= 1
        self.next_btn.disabled = page >= total

    async def _go(self, interaction: discord.Interaction, step: int):
        await interaction.response.defer()
        target = self.page + step
        if not 1 <= target <= self.total:
            return
        result = await self.render(target)
        if isinstance(result, str):
            await interaction.followup.send(result, ephemeral=True)
            return
        embeds, file, total = result
        self._set_page(target, max(total, target))
        await self._rerender(interaction, embeds, file)
