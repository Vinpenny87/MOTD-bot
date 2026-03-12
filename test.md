```csharp
using System;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Collections.Generic;
using System.Threading.Tasks;
using Discord;
using Discord.WebSocket;
using Discord.Commands;

class Program
{
    // BOT TOKEN
    static string TOKEN = Environment.GetEnvironmentVariable("DISCORD_TOKEN");

    // CONFIG
    const ulong SUBMISSIONS_CHANNEL_ID = 1197939941372608532;
    const ulong RESULTS_CHANNEL_ID = 983134844492079154;

    const string VOTE_EMOJI = "<:upvote:962050161771696148>";

    static TimeZoneInfo TIMEZONE =
        TimeZoneInfo.FindSystemTimeZoneById("Central European Standard Time");

    const int POST_HOUR = 17;
    const int POST_MINUTE = 0;

    const int TOP_N = 3;
    const bool EXCLUDE_BOT_VOTE = false;

    static HashSet<ulong> DEV_USER_IDS = new HashSet<ulong>()
    {
        1097539138959462471,
        582786439763329024
    };

    static HashSet<DayOfWeek> NON_PRO_WEEKDAYS = new HashSet<DayOfWeek>()
    {
        DayOfWeek.Monday,
        DayOfWeek.Thursday
    };

    const string NON_PRO_MESSAGE =
        "# Today is a NON-PRO day! Any minis made using pro features will be deleted!";

    static DiscordSocketClient client;
    static CommandService commands;

    static async Task Main()
    {
        if (TOKEN == null)
            throw new Exception("Set DISCORD_TOKEN environment variable first");

        client = new DiscordSocketClient(new DiscordSocketConfig
        {
            GatewayIntents =
                GatewayIntents.Guilds |
                GatewayIntents.GuildMessages |
                GatewayIntents.GuildMessageReactions |
                GatewayIntents.MessageContent
        });

        commands = new CommandService();

        client.Ready += OnReady;
        client.MessageReceived += OnMessage;

        await client.LoginAsync(TokenType.Bot, TOKEN);
        await client.StartAsync();

        _ = Task.Run(DailyLoop);

        await Task.Delay(-1);
    }

    static Task OnReady()
    {
        Console.WriteLine($"Logged in as {client.CurrentUser}");
        return Task.CompletedTask;
    }

    // Detect image attachments
    static bool IsImageAttachment(IAttachment att)
    {
        if (att.ContentType != null)
            return att.ContentType.StartsWith("image/");

        string name = att.Filename.ToLower();
        return name.EndsWith(".png") ||
               name.EndsWith(".jpg") ||
               name.EndsWith(".jpeg") ||
               name.EndsWith(".webp") ||
               name.EndsWith(".gif");
    }

    // Count votes
    static int ScoreMessage(IMessage msg)
    {
        foreach (var r in msg.Reactions)
        {
            if (r.Key.ToString() == VOTE_EMOJI)
            {
                int count = r.Value.ReactionCount;
                if (EXCLUDE_BOT_VOTE)
                    count = Math.Max(0, count - 1);
                return count;
            }
        }
        return 0;
    }

    // Scheduled window
    static (DateTime start, DateTime end) GetWindowScheduled(DateTime now)
    {
        DateTime end = new DateTime(
            now.Year,
            now.Month,
            now.Day,
            POST_HOUR,
            POST_MINUTE,
            0);

        DateTime start = end.AddDays(-1);
        return (start, end);
    }

    // Last 24h window
    static (DateTime start, DateTime end) GetWindowLast24h(DateTime now)
    {
        DateTime end = now;
        DateTime start = now.AddDays(-1);
        return (start, end);
    }

    static async Task OnMessage(SocketMessage message)
    {
        if (message.Author.IsBot)
            return;

        if (message.Channel.Id == SUBMISSIONS_CHANNEL_ID)
        {
            bool hasImage =
                message.Attachments.Any(IsImageAttachment) ||
                message.Embeds.Any(e => e.Image.HasValue || e.Thumbnail.HasValue);

            if (!hasImage)
                return;

            try
            {
                if (!message.Reactions.Any(r => r.Key.ToString() == VOTE_EMOJI))
                {
                    await message.AddReactionAsync(new Emoji(VOTE_EMOJI));
                }
            }
            catch { }
        }
    }

    static async Task RunMotdAnnouncement(bool useLast24h)
    {
        var subCh = client.GetChannel(SUBMISSIONS_CHANNEL_ID) as IMessageChannel;
        var resCh = client.GetChannel(RESULTS_CHANNEL_ID) as IMessageChannel;

        if (subCh == null || resCh == null)
            return;

        DateTime now = TimeZoneInfo.ConvertTime(DateTime.UtcNow, TIMEZONE);

        (DateTime start, DateTime end) window =
            useLast24h ? GetWindowLast24h(now) : GetWindowScheduled(now);

        Dictionary<ulong, IUserMessage> bestByAuthor = new();

        var messages = await subCh.GetMessagesAsync(limit: 1000).FlattenAsync();

        foreach (var msg in messages)
        {
            if (msg.Timestamp < window.start || msg.Timestamp > window.end)
                continue;

            if (msg.Author.IsBot)
                continue;

            int votes = ScoreMessage(msg);
            if (votes <= 0)
                continue;

            ulong author = msg.Author.Id;

            if (!bestByAuthor.ContainsKey(author) ||
                votes > ScoreMessage(bestByAuthor[author]))
            {
                bestByAuthor[author] = msg;
            }
        }

        var entries = bestByAuthor.Values.ToList();

        if (entries.Count == 0)
        {
            string text = "# No winners today";
            await resCh.SendMessageAsync(text);
            await subCh.SendMessageAsync(text);

            if (NON_PRO_WEEKDAYS.Contains(now.DayOfWeek))
                await subCh.SendMessageAsync(NON_PRO_MESSAGE);

            return;
        }

        entries = entries
            .OrderByDescending(ScoreMessage)
            .ThenBy(m => m.Timestamp)
            .ToList();

        List<(int place, IUserMessage msg)> ranked = new();

        int place = 0;
        int? lastVotes = null;

        foreach (var msg in entries)
        {
            int votes = ScoreMessage(msg);

            if (lastVotes == null || votes < lastVotes)
                place++;

            if (place > TOP_N)
                break;

            ranked.Add((place, msg));
            lastVotes = votes;
        }

        List<string> lines = new()
        {
            "# Congratulations to our :medal: MODEL OF THE DAY :medal: winners!"
        };

        foreach (var group in ranked.GroupBy(x => x.place))
        {
            int p = group.Key;
            int votes = ScoreMessage(group.First().msg);

            string emoji = p switch
            {
                1 => ":first_place:",
                2 => ":second_place:",
                _ => ":third_place:"
            };

            string label = p switch
            {
                1 => "1st",
                2 => "2nd",
                _ => "3rd"
            };

            lines.Add($"In {label} {emoji} place with {votes} upvotes");

            foreach (var m in group)
                lines.Add($"By {m.msg.Author.Mention}");
        }

        string textOut = string.Join("\n", lines);

        await resCh.SendMessageAsync(textOut);
        await subCh.SendMessageAsync(textOut);

        if (NON_PRO_WEEKDAYS.Contains(now.DayOfWeek))
            await subCh.SendMessageAsync(NON_PRO_MESSAGE);
    }

    static async Task DailyLoop()
    {
        while (true)
        {
            DateTime now = TimeZoneInfo.ConvertTime(DateTime.UtcNow, TIMEZONE);

            DateTime next =
                new DateTime(now.Year, now.Month, now.Day, POST_HOUR, POST_MINUTE, 0);

            if (next <= now)
                next = next.AddDays(1);

            TimeSpan delay = next - now;

            await Task.Delay(delay);

            await RunMotdAnnouncement(false);
        }
    }
}
```