// The emoji a project icon can be picked from: native characters only (no images, no package), a
// curated couple of hundred by category, each with the words a person searches it by. The cell
// checks every value it is sent (tares/projects/icon.py); tests/test_project_icon.py keeps this
// list in step with that check.

export interface Emoji { char: string; name: string; words: string }
export interface EmojiGroup { label: string; items: Emoji[] }

const e = (char: string, name: string, words = ""): Emoji => ({ char, name, words });

export const EMOJI_GROUPS: EmojiGroup[] = [
  { label: "Smileys", items: [
    e("😀", "Grinning face", "smile happy joy"), e("😃", "Happy face", "smile joy open"), e("😄", "Smiling face", "smile happy joy"),
    e("😁", "Beaming face", "smile grin teeth"), e("😆", "Laughing face", "laugh haha"), e("😅", "Relieved smile", "sweat phew nervous"),
    e("😂", "Tears of joy", "laugh cry lol funny"), e("🙂", "Slight smile", "smile fine ok"), e("😉", "Winking face", "wink joke"),
    e("😊", "Blushing smile", "smile warm happy"), e("😍", "Heart eyes", "love adore"), e("🤩", "Star struck", "wow amazing excited"),
    e("😘", "Blowing a kiss", "love kiss"), e("😋", "Yum", "tasty delicious food"), e("😎", "Sunglasses", "cool chill"),
    e("🤓", "Nerd", "geek glasses study"), e("🧐", "Monocle", "inspect curious review"), e("🤔", "Thinking", "think idea hmm question"),
    e("🤨", "Raised eyebrow", "doubt skeptical"), e("😐", "Neutral", "meh blank"), e("🙄", "Rolling eyes", "annoyed whatever"),
    e("😴", "Sleeping", "sleep tired zzz"), e("🤯", "Mind blown", "wow shocked explode"), e("🥳", "Party face", "celebrate birthday"),
    e("🥺", "Pleading", "please sad eyes"), e("😭", "Crying", "sad tears"), e("😤", "Huffing", "angry proud steam"),
    e("😡", "Angry", "mad rage"), e("😱", "Screaming", "scared fear shock"), e("🤖", "Robot", "bot ai machine automation"),
    e("👻", "Ghost", "boo spooky"), e("👽", "Alien", "space ufo"), e("👾", "Space invader", "game retro pixel"),
    e("💀", "Skull", "dead danger"), e("💩", "Pile of poo", "funny"), e("🤡", "Clown", "joke"),
  ] },
  { label: "People and hands", items: [
    e("👋", "Waving hand", "hello hi bye"), e("👍", "Thumbs up", "yes good like approve"), e("👎", "Thumbs down", "no bad dislike"),
    e("👏", "Clapping", "applause well done"), e("🙌", "Raised hands", "hooray celebrate"), e("🤝", "Handshake", "deal agreement partner"),
    e("🙏", "Folded hands", "please thanks pray"), e("💪", "Flexed biceps", "strong muscle power"), e("✌️", "Victory", "peace two"),
    e("🤞", "Crossed fingers", "luck hope"), e("👌", "OK hand", "perfect fine"), e("👉", "Pointing right", "next this"),
    e("✍️", "Writing hand", "write author notes"), e("🧠", "Brain", "mind think smart knowledge memory"), e("👀", "Eyes", "look watch see review"),
    e("🗣️", "Speaking head", "talk voice"), e("👤", "Person", "user profile"), e("👥", "People", "team group users"),
    e("🧑‍💻", "Technologist", "developer coder programmer engineer laptop"), e("👩‍💻", "Woman technologist", "developer coder programmer engineer"),
    e("👨‍💻", "Man technologist", "developer coder programmer engineer"), e("🧑‍🔬", "Scientist", "research lab science"),
    e("🧑‍🎨", "Artist", "design paint creative"), e("🧑‍🚀", "Astronaut", "space launch"), e("🧑‍🔧", "Mechanic", "repair fix tools"),
    e("🧙", "Mage", "wizard magic"), e("🦸", "Superhero", "hero power"), e("🥷", "Ninja", "stealth"),
  ] },
  { label: "Animals and nature", items: [
    e("🐶", "Dog", "puppy pet"), e("🐱", "Cat", "kitten pet"), e("🐭", "Mouse", "animal"), e("🐰", "Rabbit", "bunny"), e("🦊", "Fox", "animal clever"),
    e("🐻", "Bear", "animal"), e("🐼", "Panda", "animal bear"), e("🐨", "Koala", "animal"), e("🐯", "Tiger", "animal cat"), e("🦁", "Lion", "animal king"),
    e("🐮", "Cow", "animal"), e("🐷", "Pig", "animal"), e("🐸", "Frog", "animal green"), e("🐵", "Monkey", "animal"), e("🐔", "Chicken", "bird"),
    e("🐧", "Penguin", "bird linux"), e("🦉", "Owl", "bird wise night"), e("🦅", "Eagle", "bird"), e("🦆", "Duck", "bird"), e("🐝", "Bee", "honey busy insect"),
    e("🐛", "Bug", "insect error defect"), e("🦋", "Butterfly", "insect pretty"), e("🐞", "Lady beetle", "bug insect"), e("🐢", "Turtle", "slow"), e("🐍", "Snake", "python"),
    e("🐙", "Octopus", "sea github"), e("🐳", "Whale", "sea docker"), e("🐬", "Dolphin", "sea"), e("🦈", "Shark", "sea"), e("🐘", "Elephant", "postgres memory"),
    e("🦄", "Unicorn", "magic startup"), e("🐉", "Dragon", "fantasy"), e("🌲", "Evergreen tree", "forest pine"), e("🌳", "Tree", "forest nature"), e("🌴", "Palm tree", "beach holiday"),
    e("🌵", "Cactus", "desert"), e("🌱", "Seedling", "grow sprout start new"), e("🍀", "Four leaf clover", "luck"), e("🌸", "Cherry blossom", "flower spring"), e("🌻", "Sunflower", "flower"),
    e("🌹", "Rose", "flower love"), e("🍄", "Mushroom", "fungus"), e("🌍", "Globe Europe Africa", "earth world planet"), e("🌎", "Globe Americas", "earth world planet"), e("🌏", "Globe Asia", "earth world planet"),
    e("🌙", "Moon", "night crescent"), e("☀️", "Sun", "weather bright"), e("⭐", "Star", "favorite"), e("🌟", "Glowing star", "shine"), e("⚡", "Lightning", "fast zap electric power"),
    e("🔥", "Fire", "hot flame lit"), e("💧", "Droplet", "water"), e("🌊", "Wave", "water sea ocean"), e("🌈", "Rainbow", "colors"), e("☁️", "Cloud", "weather sky"),
    e("❄️", "Snowflake", "cold winter"), e("🌪️", "Tornado", "storm"),
  ] },
  { label: "Food and drink", items: [
    e("🍎", "Red apple", "fruit"), e("🍊", "Orange", "fruit"), e("🍋", "Lemon", "fruit citrus"), e("🍌", "Banana", "fruit"), e("🍉", "Watermelon", "fruit"),
    e("🍇", "Grapes", "fruit"), e("🍓", "Strawberry", "fruit"), e("🍒", "Cherries", "fruit"), e("🥑", "Avocado", "fruit"), e("🍅", "Tomato", "vegetable"),
    e("🥕", "Carrot", "vegetable"), e("🌶️", "Hot pepper", "spicy chili"), e("🌽", "Corn", "vegetable"), e("🍞", "Bread", "bakery"), e("🧀", "Cheese", "food"),
    e("🍳", "Cooking", "egg breakfast"), e("🍔", "Burger", "hamburger food"), e("🍟", "Fries", "food"), e("🍕", "Pizza", "food slice"), e("🌮", "Taco", "food mexican"),
    e("🍣", "Sushi", "food japan"), e("🍜", "Noodles", "ramen food"), e("🍪", "Cookie", "biscuit"), e("🍰", "Cake", "dessert slice"), e("🎂", "Birthday cake", "celebrate"),
    e("🍩", "Doughnut", "donut"), e("🍫", "Chocolate", "sweet"), e("☕", "Coffee", "drink morning cafe"), e("🍵", "Tea", "drink"), e("🍺", "Beer", "drink"),
    e("🍷", "Wine", "drink"), e("🥂", "Clinking glasses", "cheers celebrate"),
  ] },
  { label: "Activities", items: [
    e("⚽", "Football", "soccer sport"), e("🏀", "Basketball", "sport"), e("🏈", "American football", "sport"), e("⚾", "Baseball", "sport"), e("🎾", "Tennis", "sport"),
    e("🏐", "Volleyball", "sport"), e("🏓", "Table tennis", "ping pong sport"), e("🥊", "Boxing", "sport fight"), e("🥅", "Goal net", "target sport"), e("⛳", "Golf", "sport flag"),
    e("🏆", "Trophy", "win prize champion"), e("🥇", "Gold medal", "first winner"), e("🥈", "Silver medal", "second"), e("🥉", "Bronze medal", "third"), e("🏅", "Medal", "award"),
    e("🎯", "Bullseye", "target goal aim focus"), e("🎮", "Video game", "controller play"), e("🕹️", "Joystick", "arcade play"), e("🎲", "Game die", "dice chance random"), e("🧩", "Puzzle piece", "plugin integration fit"),
    e("♟️", "Chess pawn", "strategy game"), e("🎨", "Palette", "art design paint color"), e("🎭", "Theatre", "drama masks"), e("🎬", "Clapper board", "film movie"), e("🎤", "Microphone", "sing talk"),
    e("🎧", "Headphones", "music listen"), e("🎵", "Musical note", "music sound"), e("🎸", "Guitar", "music rock"), e("🎹", "Piano", "music keys"), e("🥁", "Drum", "music"),
    e("🎉", "Party popper", "celebrate tada release"), e("🎊", "Confetti", "celebrate"), e("🎈", "Balloon", "party"), e("🎁", "Gift", "present box"), e("🎀", "Ribbon", "bow"),
  ] },
  { label: "Travel and places", items: [
    e("🚀", "Rocket", "launch ship space startup deploy"), e("✈️", "Airplane", "flight travel"), e("🛸", "Flying saucer", "ufo space"), e("🛰️", "Satellite", "space orbit"), e("🚁", "Helicopter", "fly"),
    e("🚗", "Car", "drive vehicle"), e("🚕", "Taxi", "car"), e("🚌", "Bus", "travel"), e("🚚", "Truck", "ship move"), e("🚜", "Tractor", "farm"),
    e("🏎️", "Racing car", "fast speed"), e("🏍️", "Motorcycle", "bike"), e("🚲", "Bicycle", "bike ride"), e("🛴", "Scooter", "kick"), e("🚂", "Locomotive", "train steam"),
    e("🚆", "Train", "rail"), e("🚢", "Ship", "boat cargo"), e("⛵", "Sailboat", "sea"), e("⚓", "Anchor", "ship harbor"), e("🗺️", "Map", "world plan roadmap"),
    e("🧭", "Compass", "direction navigate"), e("🏔️", "Snow mountain", "peak"), e("⛰️", "Mountain", "peak"), e("🌋", "Volcano", "lava"), e("🏕️", "Camping", "tent"),
    e("🏖️", "Beach", "sand holiday"), e("🏝️", "Island", "desert holiday"), e("🏠", "House", "home"), e("🏡", "Garden house", "home"), e("🏢", "Office building", "work company"),
    e("🏭", "Factory", "build production industry"), e("🏗️", "Construction", "crane build"), e("🏛️", "Classical building", "museum bank"), e("🏰", "Castle", "fort"), e("🗽", "Statue of Liberty", "new york"),
    e("🌉", "Bridge", "night city"), e("🌃", "Night city", "stars"), e("🌆", "City at dusk", "sunset town"),
  ] },
  { label: "Objects", items: [
    e("💻", "Laptop", "computer code work"), e("🖥️", "Desktop computer", "monitor screen"), e("⌨️", "Keyboard", "type input"), e("🖱️", "Computer mouse", "click"), e("📱", "Mobile phone", "cell app"),
    e("🔌", "Plug", "connect power integration"), e("🔋", "Battery", "power charge"), e("💾", "Floppy disk", "save storage"), e("💿", "Disc", "cd data"), e("🗄️", "File cabinet", "archive storage database"),
    e("📁", "Folder", "files directory"), e("📂", "Open folder", "files"), e("📄", "Document", "page file"), e("📃", "Page", "document"), e("📋", "Clipboard", "list copy tasks"),
    e("📝", "Memo", "note write"), e("📓", "Notebook", "journal"), e("📚", "Books", "library read learn docs"), e("📖", "Open book", "read docs"), e("📰", "Newspaper", "news press"),
    e("🔖", "Bookmark", "save tag"), e("🏷️", "Label", "tag price"), e("📊", "Bar chart", "stats data graph metrics"), e("📈", "Chart rising", "growth up metrics"), e("📉", "Chart falling", "down loss"),
    e("🧾", "Receipt", "invoice bill"), e("💰", "Money bag", "cash finance"), e("💵", "Dollar", "money cash"), e("💳", "Credit card", "payment billing"), e("💎", "Gem", "diamond value"),
    e("🔧", "Wrench", "tool fix repair settings"), e("🔨", "Hammer", "build tool"), e("🛠️", "Hammer and wrench", "tools build fix"), e("⚙️", "Gear", "settings config engine"), e("🧰", "Toolbox", "tools kit"),
    e("🧪", "Test tube", "lab experiment test"), e("🔬", "Microscope", "science research"), e("🔭", "Telescope", "space observe"), e("💡", "Light bulb", "idea bright"), e("🔦", "Flashlight", "torch light"),
    e("🕯️", "Candle", "light"), e("🔒", "Locked", "security private"), e("🔓", "Unlocked", "open security"), e("🔑", "Key", "secret access password"), e("🛡️", "Shield", "security protect guard"),
    e("🔔", "Bell", "notify alert"), e("📣", "Megaphone", "announce marketing"), e("📢", "Loudspeaker", "announce"), e("✉️", "Envelope", "mail email"), e("📬", "Mailbox", "mail inbox"),
    e("📦", "Package", "box ship release npm"), e("🚧", "Under construction", "barrier work progress"), e("⏰", "Alarm clock", "time wake"), e("⏳", "Hourglass", "wait time"), e("📅", "Calendar", "date schedule"),
    e("🗓️", "Spiral calendar", "date plan"), e("📷", "Camera", "photo picture"), e("🎥", "Movie camera", "film video"), e("📺", "Television", "tv screen"), e("📻", "Radio", "music"),
    e("📌", "Pushpin", "pin location"), e("📍", "Round pin", "location place"), e("📎", "Paperclip", "attach"), e("✂️", "Scissors", "cut"), e("🖊️", "Pen", "write"),
    e("✏️", "Pencil", "write edit"), e("🖍️", "Crayon", "draw color"), e("🧹", "Broom", "clean tidy"), e("🧼", "Soap", "clean wash"), e("🪣", "Bucket", "storage"),
  ] },
  { label: "Symbols", items: [
    e("❤️", "Red heart", "love"), e("🧡", "Orange heart", "love"), e("💛", "Yellow heart", "love"), e("💚", "Green heart", "love"), e("💙", "Blue heart", "love"),
    e("💜", "Purple heart", "love"), e("🖤", "Black heart", "love"), e("🤍", "White heart", "love"), e("💖", "Sparkling heart", "love"), e("✨", "Sparkles", "magic shine new"),
    e("💫", "Dizzy", "star"), e("💥", "Collision", "boom explode"), e("💯", "Hundred points", "perfect score"), e("✅", "Check mark", "done yes ok"), e("☑️", "Ballot check", "done tick"),
    e("❌", "Cross mark", "no wrong delete"), e("⛔", "No entry", "stop block forbidden"), e("🚫", "Prohibited", "no forbidden"), e("⚠️", "Warning", "caution alert danger"), e("❓", "Question mark", "help"),
    e("❗", "Exclamation mark", "important"), e("♻️", "Recycling", "reuse green"), e("🔁", "Repeat", "loop again"), e("🔄", "Arrows", "refresh sync"), e("➡️", "Right arrow", "next"),
    e("⬆️", "Up arrow", "top"), e("⬇️", "Down arrow", "bottom"), e("♾️", "Infinity", "forever loop"), e("🔍", "Magnifying glass", "search find"), e("🔗", "Link", "chain url"),
    e("⚖️", "Balance scale", "legal justice"), e("☮️", "Peace", "sign"), e("🟢", "Green circle", "dot status"), e("🟡", "Yellow circle", "dot status"), e("🔴", "Red circle", "dot status"),
    e("🔵", "Blue circle", "dot"), e("🟣", "Purple circle", "dot"), e("⚫", "Black circle", "dot"), e("⚪", "White circle", "dot"), e("🟥", "Red square", "block"),
    e("🟦", "Blue square", "block"), e("🟩", "Green square", "block"), e("🟨", "Yellow square", "block"), e("⬛", "Black square", "block"), e("⬜", "White square", "block"),
    e("1️⃣", "Keycap one", "number digit"), e("2️⃣", "Keycap two", "number digit"), e("3️⃣", "Keycap three", "number digit"),
  ] },
  { label: "Flags", items: [
    e("🏁", "Chequered flag", "finish race"), e("🚩", "Triangular flag", "red issue"), e("🏴", "Black flag", "pirate"), e("🏳️", "White flag", "surrender"), e("🏳️‍🌈", "Rainbow flag", "pride"),
    e("🇺🇸", "United States", "usa america flag"), e("🇬🇧", "United Kingdom", "uk britain flag"), e("🇩🇪", "Germany", "deutschland flag"), e("🇫🇷", "France", "flag"), e("🇪🇸", "Spain", "flag"),
    e("🇮🇹", "Italy", "flag"), e("🇳🇱", "Netherlands", "dutch flag"), e("🇮🇳", "India", "flag"), e("🇯🇵", "Japan", "flag"), e("🇨🇳", "China", "flag"),
    e("🇧🇷", "Brazil", "flag"), e("🇨🇦", "Canada", "flag"), e("🇦🇺", "Australia", "flag"),
  ] },
];

export const ALL_EMOJI: Emoji[] = EMOJI_GROUPS.flatMap((g) => g.items);

/** The groups with only what matches the words typed; every word must match the name or the search words. */
export function searchEmoji(query: string): EmojiGroup[] {
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return EMOJI_GROUPS;
  const hit = (x: Emoji) => {
    const text = `${x.name} ${x.words}`.toLowerCase();
    return words.every((w) => text.includes(w));
  };
  return EMOJI_GROUPS.map((g) => ({ ...g, items: g.items.filter(hit) })).filter((g) => g.items.length > 0);
}
