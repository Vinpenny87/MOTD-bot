using NetCord;
using NetCord.Gateway;
using NetCord.Logging;
using System.Net.Mail;

GatewayClient client = new(new BotToken("Token from Discord Developer Portal"), new GatewayClientConfiguration
{
    Logger = new ConsoleLogger(),
});

client.MessageCreate += message =>
{
    Console.WriteLine(message.Content);
    return default;
};

client.MessageReactionAdd += async args =>
{
    await client.Rest.SendMessageAsync(args.ChannelId, $"<@{args.UserId}> reacted with {args.Emoji.Name}!");
};

await client.StartAsync();
await Task.Delay(-1);