using System;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Collections.Generic;
using System.Threading.Tasks;
using Discord;
using Discord.WebSocket;

class Program
{
    // Bot token
    static readonly string TOKEN = Environment.GetEnvironmentVariable("DISCORD_TOKEN");

    // Config
    const ulong SUBMISSIONS_CHANNEL_ID = 1477706294873034938;
    const ulong RESULTS_CHANNEL_ID = 1477706352963883108;

    const string VOTE_EMOJI = "<:upvote:962050161771696148>";

    static readonly TimeZoneInfo TIMEZONE =
        TimeZoneInfo.FindSystemTimeZoneById("Central European Standard Time");

    const int POST_HOUR = 17;
    const int POST_MINUTE = 0;
    const int TOP_N = 3;
    const bool EXCLUDE_BOT_VOTE = false;

    static readonly HashSet<ulong> DEV_USER_IDS = new()
    {
        1097539138959462471, // Vinpenny
        582786439763329024   // s3rm0z
    };

    static readonly HashSet<DayOfWeek> NON_PRO_WEEKDAYS = new()
    {
        DayOfWeek.Monday,
        DayOfWeek.Thursday
    };

    const string NON_PRO_MESSAGE =
        "# Today is a NON-PRO day! Any minis made using pro features will be deleted!";

    // Globals
    static DiscordSocketClient client;
    static readonly HttpClient http = new HttpClient();

    static async Task Main()
    {
        if (string.IsNullOrWhiteSpace(TOKEN))
            throw new Exception("Set DISCORD_TOKEN environment variable first");

        client = new DiscordSocketClient(new DiscordSocketConfig
        {
            GatewayIntents =
                GatewayIntents.Guilds |
                GatewayIntents.GuildMessages |
                GatewayIntents.GuildMessageReactions |
                GatewayIntents.MessageContent
        });

        client.Ready += OnReady;
        client.MessageReceived += OnMessage;

        await client.LoginAsync(TokenType.Bot, TOKEN);
        await client.StartAsync();

        _ = Task.Run(DailyLoop);

        await Task.Delay(-1);
    }

    // Helpers
    static IEmote GetVoteEmote()
    {
        if (VOTE_EMOJI.StartsWith("<:") || VOTE_EMOJI.StartsWith("<a:"))
            return Emote.Parse(VOTE_EMOJI);

        return new Emoji(VOTE_EMOJI);
    }

    static bool IsImageAttachment(IAttachment att)
    {
        if (att.ContentType != null)
            return att.ContentType.StartsWith("image/");

        string name = att.Filename.ToLowerInvariant();
        return name.EndsWith(".png")
            || name.EndsWith(".jpg")
            || name.EndsWith(".jpeg")
            || name.EndsWith(".webp")
            || name.EndsWith(".gif");
    }

    static int ScoreMessage(IUserMessage msg)
    {
        foreach (var reaction in msg.Reactions)
        {
            if (reaction.Key.ToString() == VOTE_EMOJI)
            {
                int count = reaction.Value.ReactionCount;
                if (EXCLUDE_BOT_VOTE)
                    count = Math.Max(0, count - 1);
                return count;
            }
        }

        return 0;
    }

    static (DateTimeOffset start, DateTimeOffset end) GetWindowScheduled(DateTimeOffset nowLocal)
    {
        var end = new DateTimeOffset(
            nowLocal.Year,
            nowLocal.Month,
            nowLocal.Day,
            POST_HOUR,
            POST_MINUTE,
            0,
            nowLocal.Offset
        );

        var start = end.AddDays(-1);
        return (start, end);
    }

    static (DateTimeOffset start, DateTimeOffset end) GetWindowLast24h(DateTimeOffset nowLocal)
    {
        var end = nowLocal;
        var start = end.AddDays(-1);
        return (start, end);
    }

    static async Task<byte[]> DownloadBytesAsync(string url)
    {
        return await http.GetByteArrayAsync(url);
    }

    static async Task<List<IUserMessage>> GetMessagesInWindowAsync(IMessageChannel channel, DateTimeOffset start, DateTimeOffset end)
    {
        var results = new List<IUserMessage>();
        ulong? beforeMessageId = null;

        while (true)
        {
            IEnumerable<IMessage> batch;

            if (beforeMessageId == null)
                batch = await channel.GetMessagesAsync(100).FlattenAsync();
            else
                batch = await channel.GetMessagesAsync(beforeMessageId.Value, Direction.Before, 100).FlattenAsync();

            var messages = batch
                .OfType<IUserMessage>()
                .OrderByDescending(m => m.Timestamp)
                .ToList();

            if (messages.Count == 0)
                break;

            foreach (var msg in messages)
            {
                if (msg.Timestamp < start)
                    return results;

                if (msg.Timestamp <= end && msg.Timestamp >= start)
                    results.Add(msg);
            }

            beforeMessageId = messages.Last().Id;
        }

        return results;
    }

    static async Task SendImageBlobsAsync(IMessageChannel channel, List<(string fileName, byte[] data)> imageBlobs)
    {
        if (imageBlobs.Count == 0)
            return;

        var streams = new List<MemoryStream>();
        var attachments = new List<FileAttachment>();

        try
        {
            foreach (var (fileName, data) in imageBlobs)
            {
                var stream = new MemoryStream(data);
                streams.Add(stream);
                attachments.Add(new FileAttachment(stream, fileName));
            }

            await channel.SendFilesAsync(attachments);
        }
        finally
        {
            foreach (var stream in streams)
                stream.Dispose();
        }
    }

    // Events
    static Task OnReady()
    {
        Console.WriteLine($"Logged in as {client.CurrentUser}");
        return Task.CompletedTask;
    }

    static async Task OnMessage(SocketMessage rawMessage)
    {
        if (rawMessage.Author.IsBot)
            return;

        if (rawMessage is not SocketUserMessage message)
            return;

        // Dev command: !motdtest
        if (message.Content == "!motdtest" && DEV_USER_IDS.Contains(message.Author.Id))
        {
            await RunMotdAnnouncement(useLast24h: true);
            return;
        }

        // Only react to uploaded image submissions in the submissions channel
        if (message.Channel.Id == SUBMISSIONS_CHANNEL_ID)
        {
            bool hasImage = message.Attachments.Any(IsImageAttachment);

            if (!hasImage)
                return;

            try
            {
                bool alreadyHasVote = message.Reactions.Any(r => r.Key.ToString() == VOTE_EMOJI);
                if (!alreadyHasVote)
                    await message.AddReactionAsync(GetVoteEmote());
            }
            catch
            {
                // Intentionally silent to match Python behavior
            }
        }
    }

    // Core MoTD Logic 
    static async Task RunMotdAnnouncement(bool useLast24h)
    {
        var subCh = client.GetChannel(SUBMISSIONS_CHANNEL_ID) as IMessageChannel;
        var resCh = client.GetChannel(RESULTS_CHANNEL_ID) as IMessageChannel;

        if (subCh == null || resCh == null)
            return;

        var nowUtc = DateTimeOffset.UtcNow;
        var nowLocal = TimeZoneInfo.ConvertTime(nowUtc, TIMEZONE);

        var window = useLast24h
            ? GetWindowLast24h(nowLocal)
            : GetWindowScheduled(nowLocal);

        var bestByAuthor = new Dictionary<ulong, IUserMessage>();

        var messages = await GetMessagesInWindowAsync(subCh, window.start, window.end);

        foreach (var msg in messages)
        {
            if (msg.Author.IsBot)
                continue;

            int votes = ScoreMessage(msg);
            if (votes <= 0)
                continue;

            ulong authorId = msg.Author.Id;

            if (!bestByAuthor.TryGetValue(authorId, out var currentBest))
            {
                bestByAuthor[authorId] = msg;
                continue;
            }

            int currentBestVotes = ScoreMessage(currentBest);

            if (votes > currentBestVotes ||
                (votes == currentBestVotes && msg.Timestamp < currentBest.Timestamp))
            {
                bestByAuthor[authorId] = msg;
            }
        }

        var entries = bestByAuthor.Values.ToList();

        if (entries.Count == 0)
        {
            const string text = "# No winners today";
            await resCh.SendMessageAsync(text);
            await subCh.SendMessageAsync(text);

            if (NON_PRO_WEEKDAYS.Contains(nowLocal.DayOfWeek))
                await subCh.SendMessageAsync(NON_PRO_MESSAGE);

            return;
        }

        entries = entries
            .OrderByDescending(ScoreMessage)
            .ThenBy(m => m.Timestamp)
            .ToList();

        var ranked = new List<(int place, IUserMessage msg)>();
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

        // Text post
        var lines = new List<string>
        {
            "# Congratulations to our :medal: MODEL OF THE DAY :medal: winners!"
        };

        foreach (var group in ranked.GroupBy(x => x.place).OrderBy(g => g.Key))
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

            foreach (var item in group)
                lines.Add($"By {item.msg.Author.Mention}");
        }

        string textOut = string.Join("\n", lines);

        await resCh.SendMessageAsync(textOut);
        await subCh.SendMessageAsync(textOut);

        // Image repost
        var imageBlobs = new List<(string fileName, byte[] data)>();

        for (int i = 0; i < ranked.Count; i++)
        {
            var msg = ranked[i].msg;

            // Only use the first valid image attachment from the winning post
            var attachment = msg.Attachments.FirstOrDefault(IsImageAttachment);
            if (attachment == null)
                continue;

            try
            {
                byte[] data = await DownloadBytesAsync(attachment.Url);
                if (data.Length > 0)
                    imageBlobs.Add(($"{i + 1}_{attachment.Filename}", data));
            }
            catch
            {
                // Intentionally silent to match Python behavior
            }
        }

        if (imageBlobs.Count > 0)
        {
            await SendImageBlobsAsync(resCh, imageBlobs);
            await SendImageBlobsAsync(subCh, imageBlobs);
        }

        // NON-PRO notice
        if (NON_PRO_WEEKDAYS.Contains(nowLocal.DayOfWeek))
            await subCh.SendMessageAsync(NON_PRO_MESSAGE);
    }

    // Daily loop
    static async Task DailyLoop()
    {
        while (true)
        {
            var nowLocal = TimeZoneInfo.ConvertTime(DateTimeOffset.UtcNow, TIMEZONE);

            var next = new DateTimeOffset(
                nowLocal.Year,
                nowLocal.Month,
                nowLocal.Day,
                POST_HOUR,
                POST_MINUTE,
                0,
                nowLocal.Offset
            );

            if (next <= nowLocal)
                next = next.AddDays(1);

            TimeSpan delay = next - nowLocal;
            await Task.Delay(delay);

            await RunMotdAnnouncement(useLast24h: false);
        }
    }
}