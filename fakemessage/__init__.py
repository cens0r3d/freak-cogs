from .fakemessage import FakeMessage


async def setup(bot):
    await bot.add_cog(FakeMessage(bot))
