"""The Telegram side of the club: member bot, join requests, removals and outbox delivery.

* :mod:`paidclub.bot.app` builds the ``Bot`` and ``Dispatcher``.
* :mod:`paidclub.bot.member` is what members see: plans, checkout, status, auto-renew.
* :mod:`paidclub.bot.join` approves or declines join requests by subscription.
* :mod:`paidclub.bot.chats` checks the bot's rights and keeps the join-request invite link.
* :mod:`paidclub.bot.revoker` removes expired members (the scheduler's ``AccessRevoker``).
* :mod:`paidclub.bot.sender` delivers outbox messages (the ``OutboxSender``'s ``send``).
"""
